"""Read-only hygiene reports (experimental).

A report scans the tenant, lists findings, and can export them as a pre-filled CSV for the bulk action
that fixes them — so every fix still goes through plan → review → approve.
"""
import logging
import threading
from dataclasses import dataclass, field

from .connectors import model
from .connectors.base import ConnectorError, Interrupted
from .util import dumps, loads, new_id, now_iso

log = logging.getLogger(__name__)
MAX_PER_TYPE = 20000
OWNED_TYPES = ("access_profile", "role", "entitlement", "source", "governance_group")


@dataclass
class Param:
    name: str
    label: str
    required: bool = False
    help: str = ""
    kind: str = "text"  # "identity" renders a search-and-select picker


@dataclass
class Report:
    key: str
    title: str
    description: str
    columns: list
    scan: callable
    params: list = field(default_factory=list)
    followup_action: str = None
    followup_row: callable = None
    # Columns of the follow-up action the user fills in for all rows when creating the job from findings.
    fill: list = field(default_factory=list)
    experimental: bool = True

    def to_dict(self):
        return {"key": self.key, "title": self.title, "description": self.description, "columns": self.columns,
                "params": [p.__dict__ for p in self.params], "followup_action": self.followup_action,
                "fill": [p.__dict__ for p in self.fill],
                "experimental": self.experimental}


def _row(obj, **extra):
    return {"type": obj["type"], "id": obj["id"], "name": obj["name"], "owner": obj.get("owner_name") or "",
            "owner_id": obj.get("owner") or "", **extra}


def scan_ap_missing_description(ctx):
    for ap in ctx.each("access_profile"):
        if not ap["description"].strip():
            yield _row(ap, source=ap.get("source_name") or "")


def scan_ap_no_approval(ctx):
    for ap in ctx.each("access_profile"):
        if ap["requestable"] and ap["enabled"] and not ap["approval_schemes"]:
            yield _row(ap, source=ap.get("source_name") or "")


def scan_owned_by(ctx):
    ref = ctx.params.get("identity", "").strip()
    found = ctx.connector.find_identities(ref)
    if len(found) != 1:
        raise ValueError(f"Identity '{ref}' {'was not found' if not found else 'matches several identities'}")
    owner_id = found[0]["id"]
    for type_ in OWNED_TYPES:
        for obj in ctx.each(type_):
            if obj.get("owner") == owner_id:
                yield _row(obj, source=obj.get("source_name") or "")


def scan_orphaned_owners(ctx):
    known = {}
    for type_ in OWNED_TYPES:
        for obj in ctx.each(type_):
            owner = obj.get("owner")
            if owner and owner not in known:
                known[owner] = bool(ctx.connector.find_identities(owner))
            if not owner or not known[owner]:
                yield _row(obj, source=obj.get("source_name") or "",
                           problem="no owner" if not owner else "owner identity no longer exists")


def scan_empty_groups(ctx):
    for group in ctx.each("governance_group"):
        if not group.get("member_count"):
            yield _row(group, description=group.get("description") or "")


def scan_entitlements_metadata(ctx):
    source = ctx.params.get("source", "").strip().lower()
    for ent in ctx.each("entitlement"):
        if source and (ent.get("source_name") or "").lower() != source:
            continue
        missing = [label for label, ok in (("description", ent["description"].strip()), ("owner", ent.get("owner")))
                   if not ok]
        if missing:
            yield _row(ent, source=ent.get("source_name") or "", privileged="yes" if ent["privileged"] else "no",
                       missing=", ".join(missing))


BASE = ["type", "name", "id", "owner"]
REPORTS = {r.key: r for r in (
    Report("ap_missing_description", "Access profiles without a description",
           "Requesters cannot tell what these grant. Export to 'Update access profiles' and fill in descriptions.",
           BASE + ["source"], scan_ap_missing_description,
           followup_action="access_profile.update",
           followup_row=lambda r: {"access_profile": r["id"], "description": ""},
           fill=[Param("description", "Description for all rows", help="Optional — or write one per row in review")]),
    Report("ap_no_approval", "Requestable access profiles without approvers",
           "Requests for these are granted automatically. Export to 'Update access profiles' to add approvers.",
           BASE + ["source"], scan_ap_no_approval,
           followup_action="access_profile.update",
           followup_row=lambda r: {"access_profile": r["id"], "approval_schemes": ""},
           fill=[Param("approval_schemes", "Approvers for all rows", help="e.g. manager or manager;governance_group:Finance Approvers")]),
    Report("owned_by", "Objects owned by an identity",
           "Everything a person owns — e.g. before they leave. Export to 'Reassign ownership' and fill in new owners.",
           BASE + ["source"], scan_owned_by,
           params=[Param("identity", "Identity", True, "Search by name, username or email", kind="identity")],
           followup_action="ownership.reassign",
           followup_row=lambda r: {"object_type": r["type"], "object": r["id"], "current_owner": r["owner_id"],
                                   "new_owner": ""},
           fill=[Param("new_owner", "New owner for all rows", help="Optional — or choose per row in review",
                       kind="identity")]),
    Report("orphaned_owners", "Objects without a valid owner",
           "Access profiles, roles, entitlements, sources and governance groups with no owner, or whose owner no "
           "longer exists. Export to 'Reassign ownership'.",
           BASE + ["source", "problem"], scan_orphaned_owners,
           followup_action="ownership.reassign",
           followup_row=lambda r: {"object_type": r["type"], "object": r["id"], "new_owner": ""},
           fill=[Param("new_owner", "New owner for all rows", help="Optional — or choose per row in review",
                       kind="identity")]),
    Report("empty_groups", "Governance groups without members",
           "Approvals assigned to these can never be completed. Export to 'Change governance group members'.",
           BASE + ["description"], scan_empty_groups,
           followup_action="governance_group.members",
           followup_row=lambda r: {"group": r["id"], "add": ""},
           fill=[Param("add", "Members to add to every group", help="Identities separated by ';' — or per group in review")]),
    Report("entitlements_missing_metadata", "Entitlements missing a description or owner",
           "A common audit finding. Export to 'Update entitlements' and fill in the blanks.",
           BASE + ["source", "privileged", "missing"], scan_entitlements_metadata,
           params=[Param("source", "Source name (optional)", False, "Limit the scan to one source")],
           followup_action="entitlement.update",
           followup_row=lambda r: {"source": r["source"], "entitlement": r["id"], "description": "", "owner": ""},
           fill=[Param("owner", "Owner for all rows", help="Optional — or choose per row in review", kind="identity"),
                 Param("description", "Description for all rows", help="Optional — usually better written per row")]),
)}


class ScanContext:
    def __init__(self, connector, params, abort, on_progress):
        self.connector = connector
        self.params = params
        self.abort = abort
        self.on_progress = on_progress
        self.scanned = 0

    def each(self, type_):
        for obj in self.connector.list_objects(type_, limit=MAX_PER_TYPE, abort_event=self.abort):
            if self.abort.is_set():
                raise Interrupted()
            self.scanned += 1
            if self.scanned % 100 == 0:
                self.on_progress(self.scanned)
            yield obj


class ReportRunner:
    """Runs reports in background threads and stores results in SQLite."""

    def __init__(self, db, connections):
        self.db = db
        self.connections = connections
        self._aborts = {}
        self._lock = threading.Lock()
        with db.tx() as c:  # reports interrupted by a restart cannot resume; mark them
            c.execute("UPDATE reports SET status = 'failed', error = 'Interrupted by a restart; run it again' "
                      "WHERE status = 'running'")

    def start(self, connection_id, key, params):
        report = REPORTS.get(key)
        if report is None:
            raise ValueError("Unknown report")
        params = {p.name: str((params or {}).get(p.name, "")).strip() for p in report.params}
        missing = [p.label for p in report.params if p.required and not params[p.name]]
        if missing:
            raise ValueError(f"Required: {', '.join(missing)}")
        self.connections.store.get_connection(connection_id)
        report_id = new_id("rep")
        with self.db.tx() as c:
            c.execute("INSERT INTO reports (id, connection_id, report, params, status, scanned, created_at) "
                      "VALUES (?, ?, ?, ?, 'running', 0, ?)", (report_id, connection_id, key, dumps(params), now_iso()))
        abort = threading.Event()
        with self._lock:
            self._aborts[report_id] = abort
        threading.Thread(target=self._run, args=(report_id, connection_id, report, params, abort), daemon=True,
                         name=f"report-{report_id}").start()
        return report_id

    def _update(self, report_id, **fields):
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self.db.tx() as c:
            c.execute(f"UPDATE reports SET {assignments} WHERE id = ?", (*fields.values(), report_id))

    def _run(self, report_id, connection_id, report, params, abort):
        try:
            connector = self.connections.build(connection_id, abort_event=abort)
            ctx = ScanContext(connector, params, abort, lambda n: self._update(report_id, scanned=n))
            rows = list(report.scan(ctx))
            self._update(report_id, status="completed", scanned=ctx.scanned, rows=dumps(rows), finished_at=now_iso())
        except Interrupted:
            self._update(report_id, status="cancelled", finished_at=now_iso())
        except (ConnectorError, ValueError) as exc:
            self._update(report_id, status="failed", error=str(exc), finished_at=now_iso())
        except Exception as exc:
            log.exception("Report %s crashed", report_id)
            self._update(report_id, status="failed", error=f"Unexpected error: {exc}", finished_at=now_iso())
        finally:
            with self._lock:
                self._aborts.pop(report_id, None)

    def cancel(self, report_id):
        with self._lock:
            abort = self._aborts.get(report_id)
        if abort:
            abort.set()

    def get(self, report_id, with_rows=True):
        row = self.db.query_one("SELECT * FROM reports WHERE id = ?", (report_id,))
        if row is None:
            from .store import NotFound
            raise NotFound("Report not found")
        out = dict(row)
        out["params"] = loads(out["params"], {})
        rows = loads(out.pop("rows"), [])
        out["count"] = len(rows)
        if with_rows:
            out["rows"] = rows
        definition = REPORTS.get(out["report"])
        out["title"] = definition.title if definition else out["report"]
        return out

    def list(self):
        return [self.get(r["id"], with_rows=False)
                for r in self.db.query("SELECT id FROM reports ORDER BY created_at DESC LIMIT 50")]

    def delete(self, report_id):
        self.cancel(report_id)
        with self.db.tx() as c:
            c.execute("DELETE FROM reports WHERE id = ?", (report_id,))
