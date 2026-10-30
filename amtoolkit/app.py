"""Local web server: JSON API + static single-page UI.

Security model: the server only listens on localhost. Every state-changing request needs a per-launch
CSRF token that is embedded in the page, the Host header must be a loopback name (blocks DNS rebinding),
and cross-origin requests are rejected. Secrets are never returned by the API.
"""
import getpass
import hmac
import logging
import secrets
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from . import __version__
from .actions import ACTIONS, get_action
from .connectors import ENVIRONMENTS, ConnectorError, kinds_metadata
from .csvio import CsvError, template
from .engine import JobStateError
from .service import Toolkit
from .store import NotFound

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}


def create_app(data_dir, *, allowed_hosts=None):
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
    app.config["JSON_SORT_KEYS"] = False
    toolkit = Toolkit(data_dir)
    app.extensions["toolkit"] = toolkit
    csrf_token = secrets.token_urlsafe(32)
    app.config["CSRF_TOKEN"] = csrf_token
    hosts = set(allowed_hosts or LOOPBACK)
    operator = getpass.getuser()

    # ------------------------------------------------------------------ security
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
        return resp

    @app.errorhandler(NotFound)
    def not_found(exc):
        return jsonify(error=str(exc)), 404

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

    @app.get("/static/<path:name>")
    def static_file(name):
        return send_from_directory(STATIC, name, max_age=0)

    # ------------------------------------------------------------------ meta
    @app.get("/api/meta")
    def meta():
        return jsonify(version=__version__, operator=operator, environments=ENVIRONMENTS,
                       actions=[a.to_dict() for a in ACTIONS.values()], connector_kinds=kinds_metadata())

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

    @app.delete("/api/connections/<conn_id>")
    def delete_connection(conn_id):
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

    @app.delete("/api/jobs/<job_id>")
    def delete_job(job_id):
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
        return jsonify(changed=toolkit.set_included(job_id, ids, bool(data.get("included"))))

    @app.post("/api/jobs/<job_id>/bulk")
    def bulk(job_id):
        return jsonify(changed=toolkit.bulk(job_id, body().get("op")))

    @app.post("/api/jobs/<job_id>/plan")
    def replan(job_id):
        toolkit.replan(job_id)
        return jsonify(ok=True)

    @app.get("/api/jobs/<job_id>/approval")
    def approval_preview(job_id):
        return jsonify(toolkit.approval_preview(job_id))

    @app.post("/api/jobs/<job_id>/approve")
    def approve(job_id):
        data = body()
        toolkit.approve(job_id, data.get("fingerprint", ""), data.get("confirmation"), operator)
        return jsonify(toolkit.job_view(job_id))

    @app.post("/api/jobs/<job_id>/<command>")
    def command(job_id, command):
        toolkit.store.get_job(job_id)
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

    return app
