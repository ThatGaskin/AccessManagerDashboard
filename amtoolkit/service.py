"""Use cases behind the web API. Enforces the safety rules:

* Planning is read-only. Nothing changes in the target system until a plan is approved and started.
* Approval is bound to a fingerprint of exactly what will be sent. Any edit, exclusion or re-plan revokes it.
* Rows that already ran cannot be edited. Every edit is recorded in the audit log with before/after values.
* Production connections require typing the connection name to approve.
"""
import itertools
import logging
from pathlib import Path

from . import csvio
from .actions import ACTIONS, Plan, get_action, msg
from .connectors import ConnectionManager, KINDS
from .db import Database
from .engine import Engine, JobStateError, plan_context, plan_item, recheck_duplicates
from .actions.copy import COPY_TYPES, builtin_spec, set_env_marks, with_mark
from .compare import CompareRunner, compare_one
from .connectors import AuthError, ConfigError, ConnectorError, model
from .reports import REPORTS, ReportRunner
from .store import EDITABLE_ITEM_STATUSES, NotFound, Store
from .util import loads, now_iso

log = logging.getLogger(__name__)

EDITABLE_JOB_STATUSES = ("review", "approved", "paused", "completed")
APPROVABLE_JOB_STATUSES = ("review", "paused")
# Actions whose approval_schemes name governance groups, and which can create missing ones from the CSV.
GROUP_APPROVER_ACTIONS = ("access_profile.create", "access_profile.update")


class Toolkit:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.data_dir / "toolkit.db")
        self.store = Store(self.db)
        self.connections = ConnectionManager(self.store, self.data_dir)
        self.engine = Engine(self.store, self.connections)
        self.engine.recover()
        self.reports = ReportRunner(self.db, self.connections)
        self.compare = CompareRunner(self.connections)
        set_env_marks(self.store.env_marks())

    # ---------------------------------------------------------------- connections
    def connection_view(self, conn):
        cls = KINDS.get(conn["kind"])
        return dict(conn, kind_label=cls.label if cls else conn["kind"],
                    secret_status=self.connections.secret_status(conn))

    def save_connection(self, data, conn_id=None):
        name = (data.get("name") or "").strip()
        kind = data.get("kind")
        environment = data.get("environment") or "sandbox"
        if not name:
            raise ValueError("Name is required")
        if environment not in ("sandbox", "test", "production"):
            raise ValueError("Unknown environment")
        if conn_id:
            kind = self.store.get_connection(conn_id)["kind"]
        cls = KINDS.get(kind)
        if cls is None:
            raise ValueError("Unknown connection type")
        raw = data.get("config") or {}
        config = {f.name: str(raw.get(f.name, f.default) or "").strip() for f in cls.fields}
        missing = [f.label for f in cls.fields if f.required and (not config[f.name] or "{" in config[f.name])]
        if missing:
            raise ValueError(f"Required: {', '.join(missing)}")
        if kind == "sailpoint_isc" and not config["base_url"].startswith("https://"):
            raise ValueError("API base URL must start with https://")
        if conn_id:
            if any(self.engine.is_active(j["id"]) for j in self.store.list_jobs() if j["connection_id"] == conn_id):
                raise JobStateError("A job using this connection is running; pause it first")
            conn = self.store.update_connection(conn_id, name, environment, config)
        else:
            conn = self.store.create_connection(name, kind, environment, config)
        if data.get("secret"):
            self.connections.set_secret(conn["id"], data["secret"])
        log.info("Connection '%s' (%s, %s) %s", name, kind, environment, "updated" if conn_id else "created")
        return self.connection_view(conn)

    DEMO = (("Demo Test", "test", "demo-test"), ("Demo Prod", "production", "demo-production"))

    def create_demo_environments(self):
        """Two sandbox connections whose data differs on purpose, for trying Copy and Compare. Running it
        again resets their data to the start."""
        existing = {c["name"]: c for c in self.store.list_connections() if c["kind"] == "mock"}
        busy = [j for j in self.store.list_jobs() if self.engine.is_active(j["id"])
                and j["connection_id"] in {existing[n]["id"] for n, _, _ in self.DEMO if n in existing}]
        if busy:
            raise JobStateError("A job is running on a demo environment; pause it first")
        out = []
        for name, environment, flavor in self.DEMO:
            config = {"latency_ms": "100", "failure_rate": "0", "sample_data": flavor}
            data = {"name": name, "kind": "mock", "environment": environment, "config": config}
            conn = self.save_connection(data, existing[name]["id"]) if name in existing else self.save_connection(data)
            (self.data_dir / f"sandbox_{conn['id']}.json").unlink(missing_ok=True)  # starts again from the demo data
            out.append(conn)
        log.info("Demo environments %s", "reset" if existing else "created")
        return out

    def test_connection(self, conn_id):
        connector = self.connections.build(conn_id)
        try:
            result = connector.test()
        except Exception as exc:
            log.warning("Connection test for %s failed: %s", conn_id, exc)
            raise
        log.info("Connection test for %s succeeded: %s", conn_id, result)
        return result

    # ---------------------------------------------------------------- jobs
    def create_job(self, *, name, action_key, connection_id, options, filename, data, parent_job_id=None,
                   rows=None):
        action = get_action(action_key)
        conn = self.store.get_connection(connection_id)
        options = action.clean_options(options)
        if action.source_connection:
            source_id = options.get("source_connection_id")
            if not source_id:
                raise ValueError("Choose the connection to copy from")
            try:
                source = self.store.get_connection(source_id)
            except NotFound:
                raise ValueError("The connection to copy from does not exist") from None
            if source["id"] == conn["id"]:
                raise ValueError("Copy from a different connection than the one being changed")
        warnings = []
        if rows is None:
            rows, warnings = csvio.parse(data, action)
        name = (name or "").strip() or f"{action.title} – {Path(filename or 'upload').name}"
        job_id = self.store.create_job(name[:200], action.key, conn["id"], options, filename, rows, parent_job_id)
        copying = f", copying from '{source['name']}' ({source['environment']})" if action.source_connection else ""
        self.store.add_event(job_id, "info", "created",
                             f"Job created from {filename or 'generated rows'} with {len(rows)} row(s) "
                             f"against '{conn['name']}' ({conn['environment']}){copying}")
        for warning in warnings:
            self.store.add_event(job_id, "warning", "csv", warning)
        self.store.transition_job(job_id, ("planning",), "review")
        self.engine.plan(job_id)
        return job_id

    def job_view(self, job_id):
        job = self.store.get_job(job_id)
        counts = self.store.job_counts(job_id)
        conn = self.store.get_connection(job["connection_id"])
        action = ACTIONS.get(job["action"])
        approved = self.store.is_approved(job_id)
        active = self.engine.is_active(job_id)
        status = job["status"]
        blockers = self._blockers(counts)
        missing_groups = self.store.message_values(job_id, "gg_missing") if job["action"] in GROUP_APPROVER_ACTIONS else []
        new_groups = self.store.message_values(job_id, "gg_create") if job["action"] in GROUP_APPROVER_ACTIONS else []
        allowed = {
            "edit": status in EDITABLE_JOB_STATUSES and not active,
            "replan": status in ("review", "approved", "paused") and not active,
            "approve": status in APPROVABLE_JOB_STATUSES and not approved and not blockers and not active,
            "start": status == "approved" and approved and not active,
            "resume": status == "paused" and approved and not active,
            "pause": status == "running" and active,
            "cancel": status in ("planning", "review", "approved", "running", "pausing", "paused"),
            "retry_failed": status in ("completed", "paused") and counts["status"]["failed"] > 0 and not active,
            "rollback": bool(action and action.inverse) and status in ("completed", "paused", "cancelled")
                        and counts["status"]["succeeded"] > 0 and not active,
            "delete": status not in ("planning", "running", "pausing") and not active,
            "create_groups": bool(missing_groups) and not active,
        }
        source = None
        if action and action.source_connection:
            try:
                s = self.store.get_connection(job["options"].get("source_connection_id"))
                source = {"id": s["id"], "name": s["name"], "environment": s["environment"]}
            except NotFound:
                source = {"id": None, "name": "(deleted connection)", "environment": ""}
        parent = None
        if job["parent_job_id"]:
            try:
                p = self.store.get_job(job["parent_job_id"])
                parent = {"id": p["id"], "name": p["name"], "action": p["action"], "status": p["status"]}
            except NotFound:
                pass
        return dict(job, counts=counts, approved=approved, active=active, blockers=blockers, allowed=allowed,
                    missing_governance_groups=missing_groups, new_governance_groups=new_groups, parent=parent,
                    source_connection=source,
                    action_title=action.title if action else job["action"],
                    experimental=bool(action and action.experimental),
                    connection={"id": conn["id"], "name": conn["name"], "environment": conn["environment"],
                                "kind": conn["kind"], "secret_status": self.connections.secret_status(conn)})

    @staticmethod
    def _blockers(counts):
        blockers = []
        if counts["unplanned"]:
            blockers.append(f"{counts['unplanned']} row(s) have not been planned yet")
        if counts["errors"]:
            blockers.append(f"{counts['errors']} row(s) have errors — fix or exclude them")
        if not counts["included"]:
            blockers.append("No rows are included")
        return blockers

    def _require_editable(self, job_id):
        job = self.store.get_job(job_id)
        if job["status"] not in EDITABLE_JOB_STATUSES or self.engine.is_active(job_id):
            raise JobStateError("Rows can only be changed while the job is in review, approved, paused or "
                                "completed (not while it is planning or running)")
        return job

    def _revoke_approval(self, job, reason):
        if job["approved_hash"]:
            self.store.add_event(job["id"], "warning", "approval_revoked", f"Approval revoked: {reason}")
        target = "paused" if job["status"] == "paused" else "review"
        self.store.update_job(job["id"], status=target, approved_hash=None, approved_at=None)

    def _replan(self, job, action, item_ids):
        if not item_ids:
            return
        ctx = plan_context(self.connections, job, action)
        action.seed_context(ctx, self.store.all_items(job["id"]))
        for item_id in item_ids:
            plan_item(self.store, action, ctx, self.store.get_item(job["id"], item_id), job["options"])
        recheck_duplicates(self.store, action, job["id"])

    def edit_item(self, job_id, item_id, new_values):
        job = self._require_editable(job_id)
        action = get_action(job["action"])
        item = self.store.get_item(job_id, item_id)
        if item["status"] not in EDITABLE_ITEM_STATUSES:
            raise JobStateError(f"This row is already {item['status']} and cannot be changed")
        new_values = new_values or {}
        updated = {k: str(new_values.get(k, item["input"].get(k, "")) or "").strip()[:5000]
                   for k in action.column_names()}
        changes = {k: {"from": item["input"].get(k, ""), "to": updated[k]}
                   for k in action.column_names() if updated.get(k, "") != item["input"].get(k, "")}
        if not changes:
            return self.store.get_item(job_id, item_id)
        self.store.update_item_input(item_id, updated)
        self.store.add_event(job_id, "info", "item_edited",
                             f"Line {item['row_no']} edited: {', '.join(changes)}", item_id, changes)
        self._revoke_approval(job, f"line {item['row_no']} was edited")
        self._replan(job, action, [item_id])
        self._replan(job, action, recheck_duplicates(self.store, action, job_id))
        return self.store.get_item(job_id, item_id)

    def replan_item(self, job_id, item_id):
        job = self._require_editable(job_id)
        action = get_action(job["action"])
        item = self.store.get_item(job_id, item_id)
        if item["status"] not in EDITABLE_ITEM_STATUSES:
            raise JobStateError(f"This row is already {item['status']}")
        before = (item["op"], item["payload"])
        self._replan(job, action, [item_id])
        after = self.store.get_item(job_id, item_id)
        if (after["op"], after["payload"]) != before:
            self._revoke_approval(job, f"line {item['row_no']} plan changed")
        return after

    def set_included(self, job_id, item_ids, included):
        job = self._require_editable(job_id)
        action = get_action(job["action"])
        changed = self.store.set_included(job_id, item_ids, included)
        if changed:
            verb = "included" if included else "excluded"
            self.store.add_event(job_id, "info", f"items_{verb}", f"{changed} row(s) {verb}", data={"ids": item_ids[:500]})
            self._revoke_approval(job, f"rows were {verb}")
            self._replan(job, action, recheck_duplicates(self.store, action, job_id))
        return changed

    def bulk(self, job_id, op):
        filters = {"exclude_errors": ("errors", False), "exclude_warnings": ("warnings", False),
                   "include_all": ("excluded", True)}
        if op not in filters:
            raise ValueError("Unknown bulk operation")
        filter_name, included = filters[op]
        ids = self.store.item_ids(job_id, filter_name)
        return self.set_included(job_id, ids, included) if ids else 0

    def replan(self, job_id):
        self.engine.plan(job_id)
        self.store.add_event(job_id, "info", "replan", "Plan refresh requested; approval cleared")

    def approval_preview(self, job_id, sample=25):
        view = self.job_view(job_id)
        _, items = self.store.list_items(job_id, "all", limit=100000)
        changes = [i for i in items if i["included"] and i["op"] not in ("skip", None)]
        return {
            "fingerprint": self.store.plan_fingerprint(job_id),
            "connection": view["connection"],
            "ops": view["counts"]["ops"],
            "warnings": view["counts"]["warnings"],
            "excluded": view["counts"]["excluded"],
            "blockers": view["blockers"],
            "requires_confirmation_text": view["connection"]["name"]
                if view["connection"]["environment"] == "production" else None,
            "sample": [{"row_no": i["row_no"], "op": i["op"], "target": i["target"], "payload": i["payload"]}
                       for i in changes[:sample]],
            "total_changes": len(changes),
            "experimental": view["experimental"],
        }

    def approve(self, job_id, fingerprint, confirmation=None, approver=""):
        job = self.store.get_job(job_id)
        if job["status"] not in APPROVABLE_JOB_STATUSES or self.engine.is_active(job_id):
            raise JobStateError("This job cannot be approved in its current state")
        preview = self.approval_preview(job_id, sample=0)
        if preview["blockers"]:
            raise JobStateError("; ".join(preview["blockers"]))
        if fingerprint != preview["fingerprint"]:
            raise JobStateError("The plan changed since you reviewed it. Review it again before approving.")
        expected = preview["requires_confirmation_text"]
        if expected and (confirmation or "").strip() != expected:
            raise JobStateError(f"Type the connection name '{expected}' to approve changes in production")
        target = "paused" if job["status"] == "paused" else "approved"
        self.store.update_job(job_id, status=target, approved_hash=fingerprint, approved_at=now_iso(), error=None)
        summary = ", ".join(f"{n} {op}" for op, n in sorted(preview["ops"].items()))
        self.store.add_event(job_id, "info", "approved",
                             f"Plan approved{' by ' + approver if approver else ''}: {summary}",
                             data={"fingerprint": fingerprint, "ops": preview["ops"], "approver": approver})

    def retry_failed(self, job_id):
        job = self.store.get_job(job_id)
        if job["status"] not in ("completed", "paused") or self.engine.is_active(job_id):
            raise JobStateError("Failed rows can be retried when the job is paused or completed")
        count = self.store.reset_failed_items(job_id)
        if job["status"] == "completed":
            self.store.update_job(job_id, status="approved" if self.store.is_approved(job_id) else "review",
                                  finished_at=None)
        self.store.add_event(job_id, "info", "retry_failed", f"{count} failed row(s) queued again")
        return count

    def rollback(self, job_id):
        job = self.store.get_job(job_id)
        action = get_action(job["action"])
        if not action.inverse:
            raise JobStateError("This action has no rollback")
        if self.engine.is_active(job_id):
            raise JobStateError("Pause the job before creating a rollback")
        rows = [(i["row_no"], action.rollback_row(i)) for i in self.store.all_items(job_id)]
        rows = [(n, r) for n, r in rows if r]
        if not rows:
            raise JobStateError("This job made no changes that can be rolled back")
        inverse = get_action(action.inverse)
        new_id = self.create_job(name=f"Rollback of {job['name']}", action_key=inverse.key,
                                 connection_id=job["connection_id"], options={}, filename=None, data=None,
                                 parent_job_id=job_id, rows=[(n, inverse.normalize(r)) for n, r in rows])
        self.store.add_event(job_id, "info", "rollback_created",
                             f"Rollback job created with {len(rows)} row(s)", data={"job_id": new_id})
        return new_id

    def create_missing_governance_groups(self, job_id, owner, description="", members=""):
        """Builds a 'Create governance groups' job for every group an access profile job refers to but which
        does not exist yet. It goes through the normal plan → review → approve flow."""
        job = self.store.get_job(job_id)
        names = self.store.message_values(job_id, "gg_missing")
        if not names:
            raise JobStateError("This job does not reference any missing governance groups")
        owner = (owner or "").strip()
        if not owner:
            raise ValueError("Choose an owner for the new groups (email, username or identity ID)")
        action = get_action("governance_group.create")
        rows = [(n + 1, action.normalize({"name": name, "owner": owner, "members": members or "",
                                          "description": description or f"Approvers for access profiles in '{job['name']}'"}))
                for n, name in enumerate(names)]
        new_id = self.create_job(name=f"Governance groups for {job['name']}", action_key=action.key,
                                 connection_id=job["connection_id"], options={}, filename=None, data=None,
                                 parent_job_id=job_id, rows=rows)
        self.store.add_event(job_id, "info", "groups_job_created",
                             f"Created a job to add {len(names)} missing governance group(s): {', '.join(names)}. "
                             "Re-plan this job after it has run.", data={"job_id": new_id, "groups": names})
        return new_id

    # ---------------------------------------------------------------- change history
    def object_history(self, conn_id, type_, object_id, attribute=None):
        """Changes to one object (optionally one attribute) in one environment, fetched on request: the
        toolkit's own record of what its jobs changed (exact before -> after, who approved it) and the
        tenant's audit events (who changed the object and when; values only where the event carries them)."""
        if type_ not in COPY_TYPES:
            raise ValueError("Unknown object type")
        conn = self.store.get_connection(conn_id)
        connector = self.connections.build(conn_id)
        obj = connector.get_object(type_, object_id)
        if obj is None:
            raise NotFound(f"{model.TYPE_LABELS[type_]} {object_id} not found in {conn['name']}")
        tenant, tenant_error = [], None
        try:
            for event in connector.object_events(type_, obj["id"], obj["name"]):
                changes = event.get("changes") or {}
                if attribute and changes and attribute not in changes:
                    continue
                if attribute and changes:
                    changes = {attribute: changes[attribute]}
                tenant.append(dict(event, changes=changes, values_known=bool(changes)))
        except NotImplementedError:
            tenant_error = "This connection type does not provide audit events"
        except (AuthError, ConfigError) as exc:
            tenant_error = f"Could not read audit events: {exc}"
        except ConnectorError as exc:
            tenant_error = f"Could not read audit events: {exc}"
        return {"connection": {k: conn[k] for k in ("id", "name", "environment")},
                "object": {"type": type_, "id": obj["id"], "name": obj["name"], "modified": obj.get("modified")},
                "attribute": attribute, "toolkit": self._toolkit_history(conn_id, obj["id"], attribute),
                "tenant": tenant, "tenant_error": tenant_error}

    def _toolkit_history(self, conn_id, object_id, attribute):
        rows = self.db.query(
            "SELECT i.op, i.payload, i.remote_id, i.updated_at, j.id AS job_id, j.name AS job_name, j.action "
            "FROM items i JOIN jobs j ON j.id = i.job_id WHERE j.connection_id = ? AND i.status = 'succeeded' "
            "AND (i.payload LIKE ? OR i.remote_id = ?) ORDER BY i.updated_at DESC",
            (conn_id, f'%"id":"{object_id}"%', object_id))
        approvers = {}
        out = []
        for row in rows:
            payload = loads(row["payload"], {}) or {}
            target = payload.get("object") or {}
            if row["job_id"] not in approvers:
                event = self.db.query_one("SELECT data FROM events WHERE job_id = ? AND kind = 'approved' "
                                          "ORDER BY id DESC LIMIT 1", (row["job_id"],))
                approvers[row["job_id"]] = (loads(event["data"], {}) or {}).get("approver") if event else None
            base = {"time": row["updated_at"], "job_id": row["job_id"], "job_name": row["job_name"],
                    "action": ACTIONS[row["action"]].title if row["action"] in ACTIONS else row["action"],
                    "approved_by": approvers[row["job_id"]]}
            if row["op"] == "create" and row["remote_id"] == object_id:
                out.append(dict(base, change="created", changes={}))
            elif target.get("id") == object_id and isinstance(payload.get("after"), dict):
                labels = payload.get("labels") or {}
                changes = {f: {"old": labels[f][0] if f in labels else payload["before"].get(f),
                               "new": labels[f][1] if f in labels else v}
                           for f, v in payload["after"].items() if not attribute or f == attribute}
                if changes:
                    out.append(dict(base, change="updated", changes=changes))
            elif row["op"] == "delete" and target.get("id") == object_id:
                out.append(dict(base, change="deleted", changes={}))
        return out

    def mark_env_specific(self, object_type, attribute, env_specific):
        """Marks an attribute of a type as environment-specific (kept in the target unless ticked) or not,
        for every comparison, copy and plan from now on. Marking it the way the built-in rules classify it
        removes the mark."""
        if object_type not in COPY_TYPES:
            raise ValueError("Choose an object type: " + ", ".join(COPY_TYPES))
        if not isinstance(env_specific, bool):
            raise ValueError("env_specific must be true or false")
        spec = builtin_spec(object_type, str(attribute or "")) if len(str(attribute or "")) <= 200 else None
        if spec is None:
            raise ValueError(f"Unknown attribute '{attribute}' for {model.TYPE_LABELS[object_type].lower()}s")
        if spec.secret:
            raise ValueError(f"'{spec.label}' is a secret; secrets are never copied")
        self.store.set_env_mark(object_type, spec.name, None if env_specific == spec.env_specific else env_specific)
        set_env_marks(self.store.env_marks())
        log.info("Attribute %s of %ss marked %senvironment-specific", spec.name, object_type,
                 "" if env_specific else "not ")
        spec = with_mark(object_type, spec)
        return {"object_type": object_type, "attribute": spec.name, "env_specific": spec.env_specific,
                "marked": spec.marked}

    def env_marks(self):
        return [{"object_type": t, "attribute": a, "env_specific": v}
                for t, attrs in sorted(self.store.env_marks().items()) for a, v in sorted(attrs.items())]

    def compare_csv(self, compare_id):
        """A finished comparison as CSV: one line per differing attribute, entitlement or missing object."""
        r = self.compare.get(compare_id)
        if r["status"] != "completed":
            raise JobStateError("The comparison has not finished")
        src, dst = r["source"]["name"], r["target"]["name"]

        def text(v):
            if isinstance(v, bool):
                return "yes" if v else "no"
            if isinstance(v, list):
                return "; ".join(map(str, v))
            return "" if v is None else str(v)
        lines = []
        for row in r["rows"]:
            changed = [text(row.get("source_modified")), text(row.get("target_modified")), text(row.get("newer"))]
            if row["status"] in ("new", "ambiguous"):
                lines.append([row["name"], row["status"], "", "", "", "", row.get("problem", "")] + changed)
            for d in row["diffs"] if row["status"] == "different" else []:
                lines.append([row["name"], "different", d["label"], text(d["source"]), text(d["target"]),
                              "yes" if d["env_specific"] else "", ""] + changed)
            ents = row.get("entitlements") or {}
            for name in ents.get("only_in_source", []):
                lines.append([row["name"], "entitlement", "entitlement", name, "", "", f"only in {src}"] + changed)
            for name in ents.get("only_in_target", []):
                lines.append([row["name"], "entitlement", "entitlement", "", name, "", f"only in {dst}"] + changed)
        lines += [[name, f"only in {dst}", "", "", "", "", ""] + [""] * 3 for name in r["only_in_target"]]
        headers = ["object", "status", "attribute", f"value in {src}", f"value in {dst}", "environment-specific",
                   "note", f"last changed in {src}", f"last changed in {dst}", "changed more recently in"]
        return csvio.write(headers, lines)

    def report_csv(self, report_id):
        rep = self.reports.get(report_id)
        cols = REPORTS[rep["report"]].columns
        return csvio.write(cols, [[r.get(c, "") for c in cols] for r in rep["rows"]])

    def report_followup_csv(self, report_id):
        """The report's findings as a ready-to-edit CSV for the action that fixes them."""
        rep = self.reports.get(report_id)
        definition = REPORTS[rep["report"]]
        if not definition.followup_action:
            raise ValueError("This report has no follow-up action")
        action = get_action(definition.followup_action)
        cols = action.column_names()
        rows = [definition.followup_row(r) for r in rep["rows"]]
        return action, csvio.write(cols, [[r.get(c, "") for c in cols] for r in rows])

    def create_job_from_report(self, report_id, defaults=None):
        """Turns a report's findings into a job for the action that fixes them. `defaults` fill blank cells
        for every row (e.g. one new owner). The job is planned read-only and opens in review as usual."""
        rep = self.reports.get(report_id)
        definition = REPORTS.get(rep["report"])
        if not definition or not definition.followup_action:
            raise ValueError("This report has no follow-up action")
        if rep["status"] != "completed" or not rep["rows"]:
            raise JobStateError("The report has no findings to turn into a job")
        action = get_action(definition.followup_action)
        allowed = {p.name for p in definition.fill}
        defaults = {k: str(v).strip() for k, v in (defaults or {}).items() if k in allowed and str(v or "").strip()}
        rows = []
        for n, finding in enumerate(rep["rows"], start=1):
            row = definition.followup_row(finding)
            row.update({k: v for k, v in defaults.items() if not row.get(k)})
            rows.append((n, action.normalize(row)))
        job_id = self.create_job(name=f"{action.title} — from report: {definition.title}", action_key=action.key,
                                 connection_id=rep["connection_id"], options={}, filename=f"report {report_id}",
                                 data=None, rows=rows)
        self.store.add_event(job_id, "info", "from_report",
                             f"Created from report '{definition.title}' ({len(rows)} finding(s))"
                             + (f"; filled for all rows: {', '.join(defaults)}" if defaults else ""),
                             data={"report_id": report_id, "defaults": defaults})
        return job_id

    # ---------------------------------------------------------------- copy one object between environments
    def list_object_names(self, conn_id, type_, query="", limit=50):
        """Names of objects of a type for the object picker: the first `limit`, or those containing `query`."""
        if type_ not in COPY_TYPES:
            raise ValueError("Unknown object type")
        connector = self.connections.build(conn_id)
        query = (query or "").strip()
        if query:
            names = [s["value"] for s in connector.suggest(f"object:{type_}", query, None, limit)]
        else:
            names = [o["name"] for o in itertools.islice(connector.list_objects(type_, limit=limit), limit)]
        return sorted(dict.fromkeys(n for n in names if n), key=str.lower)

    def copy_request(self, data):
        """Validates a copy request from the Copy page: two different connections, a type and an object."""
        data = data or {}
        type_ = data.get("object_type")
        if type_ not in COPY_TYPES:
            raise ValueError("Choose an object type")
        source = self.store.get_connection(str(data.get("source_connection_id") or ""))
        target = self.store.get_connection(str(data.get("connection_id") or ""))
        if source["id"] == target["id"]:
            raise ValueError("Choose two different connections")
        obj = str(data.get("object") or "").strip()
        if not obj:
            raise ValueError("Choose an object")
        attributes = [str(a) for a in data.get("attributes") or [] if str(a).strip()]
        overrides = {}
        for key, value in (data.get("overrides") or {}).items():
            value = str(value or "").strip()
            if value and (";" in value or "\n" in value):
                raise ValueError(f"The value for {key} cannot contain ';' or line breaks")
            if value:
                overrides[str(key)] = value
        return {"source_connection_id": source["id"], "connection_id": target["id"], "object_type": type_,
                "object": obj, "attributes": attributes, "overrides": overrides}

    def copy_preview(self, data):
        """Every copyable attribute of one object, its value in both environments and whether they match."""
        req = self.copy_request(data)
        return compare_one(self.connections.build(req["source_connection_id"]),
                           self.connections.build(req["connection_id"]), req["object_type"], req["object"])

    def _copy_row(self, req):
        action = get_action("object.copy")
        return action.normalize({"object_type": req["object_type"], "object": req["object"],
                                 "attributes": ";".join(req["attributes"]),
                                 "overrides": ";".join(f"{k}={v}" for k, v in req["overrides"].items())})

    def plan_copy(self, req):
        """Plans copying one object without creating a job: what would change, read-only."""
        action = get_action("object.copy")
        ctx = plan_context(self.connections, {"options": {"source_connection_id": req["source_connection_id"]},
                                              "connection_id": req["connection_id"]}, action)
        try:
            return action.plan(ctx, self._copy_row(req), {"if_missing": "create"})
        except (AuthError, ConfigError):
            raise
        except ConnectorError as exc:
            return Plan("error", req["object"], None, [msg("error", f"Lookup failed: {exc}", None, "lookup_failed")])

    def start_copy_job(self, req):
        """The job that records a move from the Copy page. Planned (waited for) but not approved or started."""
        source = self.store.get_connection(req["source_connection_id"])
        target = self.store.get_connection(req["connection_id"])
        label = model.TYPE_LABELS[req["object_type"]].lower()
        job_id = self.create_job(name=f"Copy {label} {req['object']} from {source['name']} to {target['name']}",
                                 action_key="object.copy", connection_id=target["id"],
                                 options={"source_connection_id": source["id"], "if_missing": "create"},
                                 filename="Copy page", data=None, rows=[(1, self._copy_row(req))])
        self.engine.wait(job_id)
        [item] = self.store.all_items(job_id)
        if item["target"]:  # the request names the object by its ID; the plan knows its name
            self.store.update_job(job_id, name=f"Copy {label} {item['target']} from {source['name']} to "
                                               f"{target['name']}"[:200])
        return job_id

    def create_copy_job(self, compare_id, selections, name=None):
        """Turns the objects and attributes chosen on a comparison into a 'Copy objects between environments'
        job. selections: [{"source_id": ..., "attributes": [...]}]; attributes are ignored for objects that do
        not exist in the target yet (they are created with every copyable attribute)."""
        result = self.compare.get(compare_id)
        if result["status"] != "completed":
            raise JobStateError("The comparison has not finished")
        by_id = {r["source_id"]: r for r in result["rows"]}
        action = get_action("object.copy")
        rows = []
        for sel in selections or []:
            row = by_id.get((sel or {}).get("source_id"))
            if row is None or row["status"] not in ("new", "different"):
                continue
            attributes = [str(a) for a in sel.get("attributes") or []] if row["status"] == "different" else []
            if row["status"] == "different" and not attributes:
                continue
            rows.append((len(rows) + 1, action.normalize({"object_type": result["type"], "object": row["source_id"],
                                                          "attributes": ";".join(attributes)})))
        if not rows:
            raise ValueError("Choose at least one object (and, for existing ones, at least one attribute)")
        title = name or (f"Copy {result['type_label'].lower()}s from {result['source']['name']} to "
                         f"{result['target']['name']}")
        job_id = self.create_job(name=title, action_key=action.key, connection_id=result["target"]["id"],
                                 options={"source_connection_id": result["source"]["id"], "if_missing": "create"},
                                 filename=f"comparison {compare_id}", data=None, rows=rows)
        self.store.add_event(job_id, "info", "from_comparison",
                             f"Created from a comparison of {result['type_label'].lower()}s: {len(rows)} object(s)",
                             data={"compare_id": compare_id})
        return job_id

    def delete_job(self, job_id):
        job = self.store.get_job(job_id)
        if job["status"] in ("planning", "running", "pausing") or self.engine.is_active(job_id):
            raise JobStateError("Pause or cancel the job before deleting it")
        self.store.delete_job(job_id)

    def export_csv(self, job_id):
        job = self.store.get_job(job_id)
        action = get_action(job["action"])
        cols = action.column_names()
        headers = ["line", "included", "planned_action", "target", "status", "remote_id", "attempts", "error",
                   "messages"] + cols
        rows = []
        for i in self.store.all_items(job_id):
            rows.append([i["row_no"], "yes" if i["included"] else "no", i["op"] or "", i["target"] or "",
                         i["status"] if i["included"] else "excluded", i["remote_id"] or "", i["attempts"],
                         i["error"] or "", " | ".join(f"{m['level']}: {m['text']}" for m in i["messages"])]
                        + [i["input"].get(c, "") for c in cols])
        return csvio.write(headers, rows)


__all__ = ["Toolkit", "JobStateError", "NotFound"]
