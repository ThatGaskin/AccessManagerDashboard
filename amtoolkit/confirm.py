"""Server-enforced confirmations for every job command.

Flow:
  1. The UI asks for a confirmation (`summary(...)`). The server describes exactly what the command will do,
     based on the job's current state, and returns a short-lived token signed with a per-launch secret.
  2. The user reads the summary, ticks "I have reviewed…" (and on production types the connection name).
  3. The command request must carry the token, `acknowledged: true` and the typed text. `verify(...)`
     recomputes the state: if anything relevant changed since the summary was shown (another tab, the job
     moved on, the plan was edited) the token no longer matches and the user must review a fresh summary.

The check runs on the server, so it cannot be skipped by a stale page, a double click or a script that
only knows the CSRF token.
"""
import hashlib
import hmac
import json
import secrets
import time

from .actions import ACTIONS, get_action
from .engine import JobStateError
from .util import fingerprint

TOKEN_TTL = 600  # seconds a reviewed summary stays valid

# Pause is deliberately not listed: stopping must always be one click.
JOB_COMMANDS = ("start", "resume", "cancel", "retry-failed", "rollback", "plan", "delete", "bulk", "include",
                "missing-governance-groups", "approve")
BULK_LABELS = {"exclude_errors": ("Exclude all rows with errors", "errors", False),
               "exclude_warnings": ("Exclude all rows with warnings", "warnings", False),
               "include_all": ("Include all excluded rows", "excluded", True)}


class ConfirmationRequired(JobStateError):
    """The command was sent without a valid, current confirmation."""


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


class Confirmer:
    def __init__(self, toolkit):
        self.toolkit = toolkit
        self.store = toolkit.store
        self._key = secrets.token_bytes(32)

    # ---------------------------------------------------------------- tokens
    def _sign(self, scope, target, command, state, expires):
        message = json.dumps([scope, target, command, state, expires], sort_keys=True, default=str).encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def _token(self, scope, target, command, state):
        expires = int(time.time()) + TOKEN_TTL
        return f"{expires}.{self._sign(scope, target, command, state, expires)}"

    def _check(self, scope, target, command, state, confirmation, requires_text):
        confirmation = confirmation if isinstance(confirmation, dict) else {}
        token = str(confirmation.get("token") or "")
        if confirmation.get("acknowledged") is not True or "." not in token:
            raise ConfirmationRequired("Please review the summary and tick the confirmation box first")
        expires_text, signature = token.split(".", 1)
        try:
            expires = int(expires_text)
        except ValueError:
            raise ConfirmationRequired("Invalid confirmation; review the summary again") from None
        if expires < time.time():
            raise ConfirmationRequired("The confirmation expired; review the summary again")
        expected = self._sign(scope, target, command, state, expires)
        if not hmac.compare_digest(signature, expected):
            raise ConfirmationRequired("Something changed since the summary was shown; review it again")
        if requires_text and (confirmation.get("text") or "").strip() != requires_text:
            raise ConfirmationRequired(f"Type '{requires_text}' to confirm")

    # ---------------------------------------------------------------- jobs
    def _job_state(self, job_id, command, params):
        """The parts of the job a confirmation is bound to. Kept minimal per command, so e.g. a running job's
        progress does not invalidate a 'cancel' confirmation, but any plan change invalidates 'start'."""
        job = self.store.get_job(job_id)
        counts = self.store.job_counts(job_id)
        base = {"status": job["status"], "params": params}
        if command in ("start", "resume", "approve"):
            base.update(plan=self.store.plan_fingerprint(job_id), approved=job["approved_hash"],
                        remaining=counts["remaining"])
        elif command == "retry-failed":
            base["failed"] = counts["status"]["failed"]
        elif command in ("rollback", "delete"):
            base["succeeded"] = counts["status"]["succeeded"]
        elif command in ("bulk", "include"):
            base["plan"] = self.store.plan_fingerprint(job_id)
        elif command == "missing-governance-groups":
            base["groups"] = self.store.message_values(job_id, "gg_missing")
        return job, counts, fingerprint(base)

    def job_summary(self, job_id, command, params=None):
        if command not in JOB_COMMANDS:
            raise ValueError("Unknown command")
        params = params or {}
        job, counts, state = self._job_state(job_id, command, params)
        conn = self.store.get_connection(job["connection_id"])
        action = ACTIONS.get(job["action"])
        s = counts["status"]
        target = f"{conn['name']} ({conn['environment']})"
        lines, warnings, danger, requires_text = [], [], False, None
        production = conn["environment"] == "production"
        experimental = bool(action and action.experimental)

        if command in ("start", "resume", "approve"):
            pending = self._pending_ops(job_id)
            title = {"start": "Start the run", "resume": "Resume the run", "approve": "Approve the plan"}[command]
            verb = {"start": "start making these changes", "resume": "resume making these changes",
                    "approve": "approve this plan"}[command]
            lines.append(f"Target: {target}")
            if action and action.source_connection:
                try:
                    source = self.store.get_connection(job["options"].get("source_connection_id"))
                    lines.append(f"Copying from: {source['name']} ({source['environment']}) — the plan already holds "
                                 "the values; nothing more is read from it")
                except Exception:
                    warnings.append("The connection this job copies from no longer exists")
            lines.append(f"Action: {action.title if action else job['action']}")
            lines += [f"{n} × {op}" for op, n in sorted(pending.items())] or ["No rows left to run"]
            if s["succeeded"] or s["skipped"] or s["failed"]:
                lines.append(f"Already done: {s['succeeded']} succeeded, {s['skipped']} skipped, {s['failed']} failed")
            if counts["excluded"]:
                lines.append(f"{_plural(counts['excluded'], 'row')} excluded (will not run)")
            if counts["warnings"]:
                warnings.append(f"{_plural(counts['warnings'], 'row')} have warnings — check them before continuing")
            new_groups = self.store.message_values(job_id, "gg_create")
            if new_groups:
                lines.append(f"{_plural(len(new_groups), 'governance group')} will be created from the CSV: "
                             f"{', '.join(new_groups)}")
            if command == "approve":
                lines += [f"Line {i['row_no']}: {i['op']} {i['target']}" for i in self._sample(job_id)]
                lines.append("After approving you still need to press Start. Any later edit cancels the approval.")
            else:
                lines.append("Rows run one at a time in file order. You can pause at any time.")
            if production:
                requires_text = conn["name"]
        elif command == "cancel":
            title, verb, danger = "Cancel the job", "cancel this job", True
            lines += [f"Target: {target}",
                      f"Done so far: {s['succeeded']} succeeded, {s['skipped']} skipped, {s['failed']} failed",
                      f"{_plural(counts['remaining'], 'row')} will NOT run",
                      "A cancelled job cannot be resumed. Changes already made stay; use Create rollback to undo them."]
        elif command == "retry-failed":
            title, verb = "Retry failed rows", "queue the failed rows again"
            lines.append(f"{_plural(s['failed'], 'failed row')} will be set back to pending")
            _, failed = self.store.list_items(job_id, "failed", limit=5)
            lines += [f"Line {i['row_no']} ({i['target']}): {i['error']}" for i in failed]
            lines.append("They run only after you press Start or Resume.")
        elif command == "rollback":
            inverse = get_action(action.inverse) if action and action.inverse else None
            title, verb = "Create a rollback job", "create a rollback job"
            lines += [f"{_plural(s['succeeded'], 'change')} made by this job will be put into a new "
                      f"'{inverse.title if inverse else 'rollback'}' job",
                      "Nothing changes now: the new job opens in review and must be approved and started."]
            created = self.store.created_governance_groups(job_id)
            if created:
                warnings.append(f"Governance groups created by this job are not deleted by the rollback: "
                                f"{', '.join(g['name'] for g in created)}. Delete them with a 'Delete governance "
                                "groups' job once nothing uses them.")
        elif command == "plan":
            title, verb = "Re-plan the job", "re-plan this job"
            lines += ["Every row that has not run yet is checked again with read-only lookups",
                      "Nothing is changed in the target system"]
            if job["approved_hash"]:
                warnings.append("The current approval will be cleared; you will need to approve again")
        elif command == "delete":
            title, verb, danger = "Delete the job", "delete this job from this computer", True
            lines += [f"The job, its {_plural(counts['total'], 'row')} and its audit log are removed from this computer"]
            if s["succeeded"]:
                warnings.append(f"{_plural(s['succeeded'], 'change')} already made in {target} are NOT undone. "
                                "Create a rollback first if you want to undo them.")
        elif command == "bulk":
            label, filter_name, _ = BULK_LABELS.get(params.get("op"), (None, None, None))
            if label is None:
                raise ValueError("Unknown bulk operation")
            n = len(self.store.item_ids(job_id, filter_name))
            title, verb = label, label[0].lower() + label[1:]
            lines.append(f"{_plural(n, 'row')} will be changed")
            warnings.append("Any approval is cleared; the job must be approved again")
        elif command == "include":
            ids = params.get("ids") or []
            included = bool(params.get("included"))
            title = "Include rows" if included else "Exclude rows"
            verb = title.lower()
            lines.append(f"{_plural(len(ids), 'selected row')} will be {'included' if included else 'excluded'}")
            warnings.append("Any approval is cleared; the job must be approved again")
        else:  # missing-governance-groups
            names = self.store.message_values(job_id, "gg_missing")
            title, verb = "Prepare a governance group job", "prepare this governance group job"
            lines += [f"A new 'Create governance groups' job with {_plural(len(names), 'group')}: {', '.join(names)}",
                      f"Owner: {params.get('owner') or '(missing)'}",
                      f"Members: {params.get('members') or '(none — groups without members cannot approve)'}",
                      "Nothing is created now: the new job opens in review and must be approved and started."]
        if experimental and command in ("start", "resume", "approve"):
            warnings.append("This is an EXPERIMENTAL action, tested only against the offline sandbox")
        if production and command in ("start", "resume", "approve", "cancel", "rollback"):
            warnings.append(f"PRODUCTION connection: {conn['name']}")
        return {"command": command, "title": title, "lines": lines, "warnings": warnings, "danger": danger,
                "checkbox": f"I have reviewed this summary and want to {verb}.",
                "requires_text": requires_text, "token": self._token("job", job_id, command, state),
                "expires_in": TOKEN_TTL}

    def verify_job(self, job_id, command, params, confirmation):
        _, _, state = self._job_state(job_id, command, params or {})
        job = self.store.get_job(job_id)
        conn = self.store.get_connection(job["connection_id"])
        requires_text = conn["name"] if conn["environment"] == "production" and \
            command in ("start", "resume", "approve") else None
        self._check("job", job_id, command, state, confirmation, requires_text)
        self.store.add_event(job_id, "info", "confirmed", f"Confirmed '{command}' after reviewing the summary",
                             data={"command": command})

    def _pending_ops(self, job_id):
        ops = {}
        for row in self.store.db.query("SELECT op, COUNT(*) AS n FROM items WHERE job_id = ? AND included = 1 "
                                       "AND status = 'pending' GROUP BY op", (job_id,)):
            ops[row["op"] or "unplanned"] = row["n"]
        return ops

    def _sample(self, job_id, limit=10):
        _, items = self.store.list_items(job_id, "all", limit=500)
        return [i for i in items if i["included"] and i["op"] not in ("skip", None, "error")][:limit]

    # ---------------------------------------------------------------- copy page
    @staticmethod
    def _copy_state(op, payload):
        return fingerprint({"op": op, "payload": payload})

    def copy_summary(self, req, plan):
        """Confirmation of a move from the Copy page, bound to the request and exactly the planned change."""
        target = self.store.get_connection(req["connection_id"])
        source = self.store.get_connection(req["source_connection_id"])
        production = target["environment"] == "production"
        verb = {"create": "Create", "update": "Update"}[plan.op]
        lines = [f"{verb} {plan.target} in {target['name']} ({target['environment']})",
                 f"Values from: {source['name']} ({source['environment']})"]
        lines += [m["text"] for m in plan.messages if m.get("code") in ("change", "copy_create", "copy_pending")]
        lines.append("It is recorded as a job, so you can undo it from the job page with Create rollback.")
        warnings = [m["text"] for m in plan.messages if m["level"] == "warning"]
        warnings.append("This is an EXPERIMENTAL action, tested only against the offline sandbox")
        if production:
            warnings.append(f"PRODUCTION connection: {target['name']}")
        return {"command": "move", "title": f"Move values to {target['name']}", "lines": lines, "warnings": warnings,
                "danger": False, "checkbox": "I have reviewed this summary and want to make these changes now.",
                "requires_text": target["name"] if production else None,
                "token": self._token("copy", fingerprint(req), "move", self._copy_state(plan.op, plan.payload)),
                "expires_in": TOKEN_TTL}

    def verify_copy(self, req, item, confirmation):
        """The move's job was planned again just now; it must plan exactly what the user confirmed."""
        target = self.store.get_connection(req["connection_id"])
        requires_text = target["name"] if target["environment"] == "production" else None
        self._check("copy", fingerprint(req), "move", self._copy_state(item["op"], item["payload"]), confirmation,
                    requires_text)

    # ---------------------------------------------------------------- connections
    def _connection_state(self, conn_id):
        conn = self.store.get_connection(conn_id)
        return conn, fingerprint({"name": conn["name"], "updated": conn["updated_at"]})

    def connection_summary(self, conn_id):
        conn, state = self._connection_state(conn_id)
        jobs = [j for j in self.store.list_jobs() if j["connection_id"] == conn_id]
        lines = [f"Connection '{conn['name']}' ({conn['environment']}) is removed from this computer",
                 "Nothing changes in the target system"]
        warnings = [f"{_plural(len(jobs), 'job')} still use it; delete them first"] if jobs else []
        return {"command": "delete", "title": "Delete the connection", "lines": lines, "warnings": warnings,
                "danger": True, "checkbox": "I have reviewed this summary and want to delete this connection.",
                "requires_text": None, "token": self._token("connection", conn_id, "delete", state),
                "expires_in": TOKEN_TTL}

    def verify_connection(self, conn_id, confirmation):
        _, state = self._connection_state(conn_id)
        self._check("connection", conn_id, "delete", state, confirmation, None)
