"""Use cases behind the web API. Enforces the safety rules:

* Planning is read-only. Nothing changes in the target system until a plan is approved and started.
* Approval is bound to a fingerprint of exactly what will be sent. Any edit, exclusion or re-plan revokes it.
* Rows that already ran cannot be edited. Every edit is recorded in the audit log with before/after values.
* Production connections require typing the connection name to approve.
"""
import logging
from pathlib import Path

from . import csvio
from .actions import ACTIONS, PlanContext, get_action
from .connectors import ConnectionManager, KINDS
from .db import Database
from .engine import Engine, JobStateError, plan_item, recheck_duplicates
from .store import EDITABLE_ITEM_STATUSES, NotFound, Store
from .util import now_iso

log = logging.getLogger(__name__)

EDITABLE_JOB_STATUSES = ("review", "approved", "paused", "completed")
APPROVABLE_JOB_STATUSES = ("review", "paused")


class Toolkit:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.data_dir / "toolkit.db")
        self.store = Store(self.db)
        self.connections = ConnectionManager(self.store, self.data_dir)
        self.engine = Engine(self.store, self.connections)
        self.engine.recover()

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
        return self.connection_view(conn)

    def test_connection(self, conn_id):
        connector = self.connections.build(conn_id)
        return connector.test()

    # ---------------------------------------------------------------- jobs
    def create_job(self, *, name, action_key, connection_id, options, filename, data, parent_job_id=None,
                   rows=None):
        action = get_action(action_key)
        conn = self.store.get_connection(connection_id)
        warnings = []
        if rows is None:
            rows, warnings = csvio.parse(data, action)
        name = (name or "").strip() or f"{action.title} – {Path(filename or 'upload').name}"
        job_id = self.store.create_job(name[:200], action.key, conn["id"], action.clean_options(options),
                                       filename, rows, parent_job_id)
        self.store.add_event(job_id, "info", "created",
                             f"Job created from {filename or 'generated rows'} with {len(rows)} row(s) "
                             f"against '{conn['name']}' ({conn['environment']})")
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
        missing_groups = self.store.message_values(job_id, "gg_missing") if job["action"] == "access_profile.create" else []
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
        parent = None
        if job["parent_job_id"]:
            try:
                p = self.store.get_job(job["parent_job_id"])
                parent = {"id": p["id"], "name": p["name"], "action": p["action"], "status": p["status"]}
            except NotFound:
                pass
        return dict(job, counts=counts, approved=approved, active=active, blockers=blockers, allowed=allowed,
                    missing_governance_groups=missing_groups, parent=parent,
                    action_title=action.title if action else job["action"],
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
        ctx = PlanContext(self.connections.build(job["connection_id"]))
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
                             data={"fingerprint": fingerprint, "ops": preview["ops"]})

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
            raise JobStateError("Nothing was created by this job, so there is nothing to roll back")
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
