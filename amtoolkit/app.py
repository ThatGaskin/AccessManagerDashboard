"""Local web server: JSON API + static single-page UI.

Security model: the server only listens on localhost. Every state-changing request needs a per-launch
CSRF token that is embedded in the page, the Host header must be a loopback name (blocks DNS rebinding),
and cross-origin requests are rejected. Secrets are never returned by the API.
"""
import getpass
import hmac
import logging
import mimetypes
import re
import secrets
import time
from importlib import resources
from pathlib import Path

from flask import Flask, Response, abort, g, jsonify, request

from . import __version__, logs
from .actions import ACTIONS, get_action
from .connectors import ENVIRONMENTS, ConnectorError, kinds_metadata
from .csvio import CsvError, template
from .confirm import ConfirmationRequired, Confirmer
from .engine import JobStateError
from .reports import REPORTS
from .service import Toolkit
from .store import NotFound

log = logging.getLogger(__name__)
# Read through importlib.resources so the UI also loads when the package runs from a .pyz zip file.
STATIC = resources.files(__package__) / "static"
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
SEARCH_KINDS = {"identity", "source", "access_profile", "governance_group", "entitlement", "object:access_profile",
                "object:role", "object:entitlement", "object:governance_group", "object:source"}


def create_app(data_dir, *, allowed_hosts=None):
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    app.config["JSON_SORT_KEYS"] = False
    toolkit = Toolkit(data_dir)
    app.extensions["toolkit"] = toolkit
    csrf_token = secrets.token_urlsafe(32)
    app.config["CSRF_TOKEN"] = csrf_token
    hosts = set(allowed_hosts or LOOPBACK)
    confirmer = Confirmer(toolkit)
    app.extensions["confirmer"] = confirmer
    operator = getpass.getuser()

    # ------------------------------------------------------------------ security
    @app.before_request
    def started():
        g.started = time.monotonic()

    @app.before_request
    def guard():
        host = (request.host or "").rsplit(":", 1)[0] if not request.host.startswith("[") \
            else request.host.split("]")[0] + "]"
        if host not in hosts:
            abort(400, "Unexpected Host header")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] != request.host:
                abort(403, "Cross-origin request rejected")
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), csrf_token):
                abort(403, "Missing or invalid CSRF token; reload the page")

    @app.after_request
    def headers(resp):
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; "
                                                   "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        if request.path.startswith("/api/") and request.path != "/api/logs":
            # Changes are logged at INFO; the UI's polling GETs only at DEBUG so they do not drown them out.
            level = logging.WARNING if resp.status_code >= 400 else \
                logging.DEBUG if request.method == "GET" else logging.INFO
            log.log(level, "%s %s -> %s (%.0f ms)", request.method, request.full_path.rstrip("?"),
                    resp.status_code, (time.monotonic() - g.get("started", time.monotonic())) * 1000)
        return resp

    @app.errorhandler(NotFound)
    def not_found(exc):
        return jsonify(error=str(exc)), 404

    @app.errorhandler(ConfirmationRequired)
    def confirmation_required(exc):
        return jsonify(error=str(exc), code="confirmation_required"), 428

    @app.errorhandler(JobStateError)
    def conflict(exc):
        return jsonify(error=str(exc)), 409

    @app.errorhandler(CsvError)
    @app.errorhandler(ValueError)
    @app.errorhandler(KeyError)
    def bad_request(exc):
        text = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        return jsonify(error=text), 400

    @app.errorhandler(ConnectorError)
    def upstream(exc):
        return jsonify(error=str(exc)), 502

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(413)
    def http_error(exc):
        if request.path.startswith("/api/") or exc.code != 404:
            return jsonify(error=getattr(exc, "description", str(exc))), exc.code
        return exc

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400, "Expected a JSON object")
        return data

    # ------------------------------------------------------------------ UI
    @app.get("/")
    def index():
        html = (STATIC / "index.html").read_text("utf-8").replace("{{CSRF_TOKEN}}", csrf_token)
        return Response(html, mimetype="text/html")

    static_files = {f.name: f for f in STATIC.iterdir() if f.is_file() and f.name != "index.html"}

    @app.get("/static/<name>")
    def static_file(name):
        resource = static_files.get(name)
        if resource is None:
            abort(404)
        mimetype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return Response(resource.read_bytes(), mimetype=mimetype, headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ meta
    @app.get("/api/meta")
    def meta():
        return jsonify(version=__version__, operator=operator, environments=ENVIRONMENTS,
                       actions=[a.to_dict() for a in ACTIONS.values()], connector_kinds=kinds_metadata(),
                       reports=[r.to_dict() for r in REPORTS.values()])

    @app.get("/api/actions/<key>/template.csv")
    def action_template(key):
        action = get_action(key)
        filename = key.replace(".", "_") + "_template.csv"
        return Response(template(action), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    # ------------------------------------------------------------------ connections
    @app.get("/api/connections")
    def list_connections():
        return jsonify([toolkit.connection_view(c) for c in toolkit.store.list_connections()])

    @app.post("/api/connections")
    def create_connection():
        return jsonify(toolkit.save_connection(body())), 201

    @app.put("/api/connections/<conn_id>")
    def update_connection(conn_id):
        return jsonify(toolkit.save_connection(body(), conn_id))

    @app.post("/api/demo-environments")
    def demo_environments():
        return jsonify(toolkit.create_demo_environments()), 201

    @app.post("/api/connections/<conn_id>/confirm")
    def confirm_connection(conn_id):
        return jsonify(confirmer.connection_summary(conn_id))

    @app.delete("/api/connections/<conn_id>")
    def delete_connection(conn_id):
        confirmer.verify_connection(conn_id, (request.get_json(silent=True) or {}).get("confirmation"))
        toolkit.store.delete_connection(conn_id)
        toolkit.connections.forget(conn_id)
        return jsonify(ok=True)

    @app.post("/api/connections/<conn_id>/secret")
    def set_secret(conn_id):
        toolkit.store.get_connection(conn_id)
        secret = body().get("secret") or ""
        if secret:
            toolkit.connections.set_secret(conn_id, secret)
        else:
            toolkit.connections.clear_secret(conn_id)
        return jsonify(toolkit.connection_view(toolkit.store.get_connection(conn_id)))

    @app.get("/api/connections/<conn_id>/identities")
    def search_identities(conn_id):
        query = request.args.get("q", "").strip()
        if len(query) < 2:
            return jsonify([])
        return jsonify(toolkit.connections.build(conn_id).search_identities(query[:100]))

    @app.get("/api/connections/<conn_id>/search")
    def search_references(conn_id):
        """Typeahead for reference cells: objects of one kind whose name contains the typed text."""
        kind = request.args.get("kind", "")
        query = request.args.get("q", "").strip()[:100]
        if kind not in SEARCH_KINDS:
            abort(400, "Unknown kind")
        if len(query) < 2:
            return jsonify([])
        connector = toolkit.connections.build(conn_id)
        scope = None
        if kind in ("entitlement", "object:entitlement") and request.args.get("scope"):
            sources = connector.find_sources(request.args["scope"].strip()[:255])
            if len(sources) != 1:
                return jsonify([])
            scope = sources[0]["id"]
        return jsonify(connector.suggest(kind, query, scope, 10))

    @app.post("/api/connections/<conn_id>/test")
    def test_connection(conn_id):
        try:
            return jsonify(ok=True, message=toolkit.test_connection(conn_id))
        except ConnectorError as exc:
            return jsonify(ok=False, message=str(exc))

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    def list_jobs():
        return jsonify([toolkit.job_view(j["id"]) for j in toolkit.store.list_jobs()])

    @app.post("/api/jobs")
    def create_job():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            abort(400, "Choose a CSV file")
        options = {k[4:]: v for k, v in request.form.items() if k.startswith("opt_")}
        job_id = toolkit.create_job(name=request.form.get("name"), action_key=request.form.get("action", ""),
                                    connection_id=request.form.get("connection_id", ""), options=options,
                                    filename=Path(upload.filename).name, data=upload.read())
        return jsonify(id=job_id), 201

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id):
        return jsonify(toolkit.job_view(job_id))

    @app.post("/api/jobs/<job_id>/confirm")
    def confirm_job(job_id):
        data = body()
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        return jsonify(confirmer.job_summary(job_id, data.get("command", ""), params))

    def confirmed(job_id, command, params=None):
        """Every job command must carry a confirmation issued for exactly this command and job state."""
        data = request.get_json(silent=True) or {}
        confirmer.verify_job(job_id, command, params or {}, data.get("confirmation"))

    @app.delete("/api/jobs/<job_id>")
    def delete_job(job_id):
        confirmed(job_id, "delete")
        toolkit.delete_job(job_id)
        return jsonify(ok=True)

    @app.get("/api/jobs/<job_id>/items")
    def list_items(job_id):
        toolkit.store.get_job(job_id)
        offset = max(0, request.args.get("offset", 0, type=int))
        limit = min(500, max(1, request.args.get("limit", 100, type=int)))
        total, items = toolkit.store.list_items(job_id, request.args.get("filter", "all"),
                                                request.args.get("q", "").strip(), offset, limit)
        return jsonify(total=total, offset=offset, items=items)

    @app.get("/api/jobs/<job_id>/items/<int:item_id>")
    def get_item(job_id, item_id):
        return jsonify(toolkit.store.get_item(job_id, item_id))

    @app.put("/api/jobs/<job_id>/items/<int:item_id>")
    def edit_item(job_id, item_id):
        values = body().get("input")
        if not isinstance(values, dict):
            abort(400, "Expected 'input' object")
        return jsonify(toolkit.edit_item(job_id, item_id, values))

    @app.post("/api/jobs/<job_id>/items/<int:item_id>/replan")
    def replan_item(job_id, item_id):
        return jsonify(toolkit.replan_item(job_id, item_id))

    @app.post("/api/jobs/<job_id>/include")
    def include(job_id):
        data = body()
        ids = [int(i) for i in data.get("ids", [])][:10000]
        if not ids:
            abort(400, "No rows selected")
        included = bool(data.get("included"))
        if not data.get("single_row"):  # toggling one row inside its editor is part of editing that row
            confirmed(job_id, "include", {"ids": ids, "included": included})
        return jsonify(changed=toolkit.set_included(job_id, ids, included))

    @app.post("/api/jobs/<job_id>/bulk")
    def bulk(job_id):
        op = body().get("op")
        confirmed(job_id, "bulk", {"op": op})
        return jsonify(changed=toolkit.bulk(job_id, op))

    @app.post("/api/jobs/<job_id>/plan")
    def replan(job_id):
        confirmed(job_id, "plan")
        toolkit.replan(job_id)
        return jsonify(ok=True)

    @app.post("/api/jobs/<job_id>/missing-governance-groups")
    def missing_groups(job_id):
        data = body()
        params = {k: str(data.get(k) or "") for k in ("owner", "members", "description")}
        confirmed(job_id, "missing-governance-groups", params)
        new_id = toolkit.create_missing_governance_groups(job_id, params["owner"], params["description"],
                                                          params["members"])
        return jsonify(id=new_id), 201

    @app.get("/api/jobs/<job_id>/approval")
    def approval_preview(job_id):
        return jsonify(toolkit.approval_preview(job_id))

    @app.post("/api/jobs/<job_id>/approve")
    def approve(job_id):
        data = body()
        confirmed(job_id, "approve")  # bound to the plan fingerprint the summary was built from
        typed = (data.get("confirmation") or {}).get("text") if isinstance(data.get("confirmation"), dict) else None
        toolkit.approve(job_id, toolkit.store.plan_fingerprint(job_id), typed, operator)
        return jsonify(toolkit.job_view(job_id))

    @app.post("/api/jobs/<job_id>/<command>")
    def command(job_id, command):
        toolkit.store.get_job(job_id)
        if command not in ("start", "resume", "pause", "cancel", "retry-failed", "rollback"):
            abort(404)
        if command != "pause":  # pausing must always be one click
            confirmed(job_id, command)
        if command in ("start", "resume"):
            toolkit.engine.start(job_id)
        elif command == "pause":
            toolkit.engine.pause(job_id)
        elif command == "cancel":
            toolkit.engine.cancel(job_id)
        elif command == "retry-failed":
            toolkit.retry_failed(job_id)
        elif command == "rollback":
            return jsonify(id=toolkit.rollback(job_id)), 201
        else:
            abort(404)
        return jsonify(toolkit.job_view(job_id))

    @app.get("/api/logs")
    def backend_logs():
        return jsonify(logs.memory.since(request.args.get("after", 0, type=int), request.args.get("level", "DEBUG"),
                                         min(2000, request.args.get("limit", 500, type=int))))

    @app.get("/api/jobs/<job_id>/events")
    def events(job_id):
        toolkit.store.get_job(job_id)
        return jsonify(toolkit.store.list_events(job_id, request.args.get("after", 0, type=int),
                                                 min(1000, request.args.get("limit", 200, type=int))))

    @app.get("/api/jobs/<job_id>/export.csv")
    def export(job_id):
        job = toolkit.store.get_job(job_id)
        return Response(toolkit.export_csv(job_id), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{job["id"]}_results.csv"'})

    # ------------------------------------------------------------------ copy one object (Copy page)
    @app.get("/api/connections/<conn_id>/objects")
    def object_names(conn_id):
        toolkit.store.get_connection(conn_id)
        return jsonify(toolkit.list_object_names(conn_id, request.args.get("type", ""), request.args.get("q", "")))

    @app.get("/api/connections/<conn_id>/history")
    def object_history(conn_id):
        return jsonify(toolkit.object_history(conn_id, request.args.get("type", ""), request.args.get("id", ""),
                                              request.args.get("attribute") or None))

    @app.post("/api/copy/preview")
    def copy_preview():
        return jsonify(toolkit.copy_preview(body()))

    @app.post("/api/copy/plan")
    def copy_plan():
        req = toolkit.copy_request(body())
        plan = toolkit.plan_copy(req)
        summary = confirmer.copy_summary(req, plan) if plan.op in ("create", "update") else None
        return jsonify(op=plan.op, target=plan.target, messages=plan.messages, confirmation=summary)

    @app.post("/api/copy/move")
    def copy_move():
        data = body()
        req = toolkit.copy_request(data)
        job_id = toolkit.start_copy_job(req)
        [item] = toolkit.store.all_items(job_id)
        try:
            confirmer.verify_copy(req, item, data.get("confirmation"))
        except ConfirmationRequired:
            toolkit.delete_job(job_id)  # nothing ran; the values changed since they were reviewed
            raise
        toolkit.store.add_event(job_id, "info", "confirmed", "Move confirmed on the Copy page after reviewing the "
                                                             "summary", data={"command": "move"})
        typed = (data.get("confirmation") or {}).get("text") if isinstance(data.get("confirmation"), dict) else None
        toolkit.approve(job_id, toolkit.store.plan_fingerprint(job_id), typed, operator)
        toolkit.engine.start(job_id)
        return jsonify(toolkit.job_view(job_id)), 201

    # ------------------------------------------------------------------ compare environments (read-only)
    @app.post("/api/compare")
    def start_compare():
        data = body()
        compare_id = toolkit.compare.start(data.get("source_connection_id", ""), data.get("connection_id", ""),
                                           data.get("object_type", ""))
        return jsonify(id=compare_id), 201

    @app.get("/api/compare/<compare_id>")
    def get_compare(compare_id):
        return jsonify(toolkit.compare.get(compare_id))

    @app.get("/api/compare/<compare_id>/export.csv")
    def export_compare(compare_id):
        result = toolkit.compare.get(compare_id)
        name = re.sub(r"[^\w.-]+", "_", f"compare_{result['type']}s_{result['source']['name']}_{result['target']['name']}") + ".csv"
        return Response(toolkit.compare_csv(compare_id), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/compare/<compare_id>/cancel")
    def cancel_compare(compare_id):
        toolkit.compare.get(compare_id)
        toolkit.compare.cancel(compare_id)
        return jsonify(ok=True)

    @app.post("/api/compare/<compare_id>/create-job")
    def create_job_from_compare(compare_id):
        data = body()
        selections = data.get("selections")
        job_id = toolkit.create_copy_job(compare_id, selections if isinstance(selections, list) else [],
                                         (data.get("name") or "").strip() or None)
        return jsonify(id=job_id), 201

    # ------------------------------------------------------------------ reports (experimental, read-only)
    @app.get("/api/reports")
    def list_reports():
        return jsonify(toolkit.reports.list())

    @app.post("/api/reports")
    def run_report():
        data = body()
        report_id = toolkit.reports.start(data.get("connection_id", ""), data.get("report", ""), data.get("params"))
        return jsonify(id=report_id), 201

    @app.get("/api/reports/<report_id>")
    def get_report(report_id):
        rep = toolkit.reports.get(report_id)
        rep["rows"] = rep["rows"][:1000]  # the CSV export has everything
        return jsonify(rep)

    @app.post("/api/reports/<report_id>/cancel")
    def cancel_report(report_id):
        toolkit.reports.get(report_id, with_rows=False)
        toolkit.reports.cancel(report_id)
        return jsonify(ok=True)

    @app.delete("/api/reports/<report_id>")
    def delete_report(report_id):
        toolkit.reports.delete(report_id)
        return jsonify(ok=True)

    @app.get("/api/reports/<report_id>/export.csv")
    def export_report(report_id):
        return Response(toolkit.report_csv(report_id), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{report_id}.csv"'})

    @app.post("/api/reports/<report_id>/create-job")
    def create_job_from_report(report_id):
        defaults = body().get("defaults")
        job_id = toolkit.create_job_from_report(report_id, defaults if isinstance(defaults, dict) else {})
        return jsonify(id=job_id), 201

    @app.get("/api/reports/<report_id>/followup.csv")
    def followup_report(report_id):
        action, text = toolkit.report_followup_csv(report_id)
        name = f"{report_id}_{action.key.replace('.', '_')}.csv"
        return Response(text, mimetype="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return app
