"""Data access for connections, jobs, items and the audit log."""
from .db import Database
from .util import dumps, fingerprint, loads, new_id, now_iso

# Job lifecycle:
#   planning -> review -> approved -> running <-> pausing -> paused -> (approved again) -> running -> completed
#   Any non-running state can go to cancelled. Edits send approved/paused jobs back for re-approval.
JOB_STATUSES = ("planning", "review", "approved", "running", "pausing", "paused", "completed", "cancelled")
ACTIVE_STATUSES = ("planning", "running", "pausing")
ITEM_STATUSES = ("pending", "running", "succeeded", "skipped", "failed")
EXECUTABLE_OPS = ("create", "delete", "update", "skip")
# Items that have been run (or are being run) can no longer be edited.
EDITABLE_ITEM_STATUSES = ("pending", "failed")
IN_DOUBT = dumps({"in_doubt": True})


class NotFound(Exception):
    pass


def _job(row):
    if row is None:
        return None
    job = dict(row)
    job["options"] = loads(job["options"], {})
    return job


def _item(row):
    if row is None:
        return None
    item = dict(row)
    for key, default in (("original", {}), ("input", {}), ("payload", None), ("messages", []), ("result", None)):
        item[key] = loads(item[key], default)
    item["included"] = bool(item["included"])
    return item


def _connection(row):
    if row is None:
        return None
    conn = dict(row)
    conn["config"] = loads(conn["config"], {})
    return conn


class Store:
    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------------- connections
    def list_connections(self):
        return [_connection(r) for r in self.db.query("SELECT * FROM connections ORDER BY name")]

    def get_connection(self, conn_id):
        conn = _connection(self.db.query_one("SELECT * FROM connections WHERE id = ?", (conn_id,)))
        if conn is None:
            raise NotFound(f"Connection {conn_id} not found")
        return conn

    def create_connection(self, name, kind, environment, config):
        conn_id = new_id("con")
        ts = now_iso()
        with self.db.tx() as c:
            c.execute("INSERT INTO connections (id, name, kind, environment, config, created_at, updated_at) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)", (conn_id, name, kind, environment, dumps(config), ts, ts))
        return self.get_connection(conn_id)

    def update_connection(self, conn_id, name, environment, config):
        self.get_connection(conn_id)
        with self.db.tx() as c:
            c.execute("UPDATE connections SET name = ?, environment = ?, config = ?, updated_at = ? WHERE id = ?",
                      (name, environment, dumps(config), now_iso(), conn_id))
        return self.get_connection(conn_id)

    def delete_connection(self, conn_id):
        used = self.db.query_one("SELECT COUNT(*) FROM jobs WHERE connection_id = ?", (conn_id,))[0]
        if used:
            raise ValueError(f"Connection is used by {used} job(s); delete those jobs first")
        with self.db.tx() as c:
            c.execute("DELETE FROM connections WHERE id = ?", (conn_id,))

    # ---------------------------------------------------------------- jobs
    def create_job(self, name, action, connection_id, options, source_file, rows, parent_job_id=None):
        """`rows` is a list of (row_no, normalized_row_dict)."""
        job_id = new_id("job")
        ts = now_iso()
        with self.db.tx() as c:
            c.execute("INSERT INTO jobs (id, name, action, connection_id, status, options, source_file, "
                      "parent_job_id, created_at, updated_at) VALUES (?, ?, ?, ?, 'planning', ?, ?, ?, ?, ?)",
                      (job_id, name, action, connection_id, dumps(options), source_file, parent_job_id, ts, ts))
            c.executemany("INSERT INTO items (job_id, row_no, original, input, updated_at) VALUES (?, ?, ?, ?, ?)",
                          [(job_id, row_no, dumps(row), dumps(row), ts) for row_no, row in rows])
        return job_id

    def get_job(self, job_id):
        job = _job(self.db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,)))
        if job is None:
            raise NotFound(f"Job {job_id} not found")
        return job

    def list_jobs(self):
        return [_job(r) for r in self.db.query("SELECT * FROM jobs ORDER BY created_at DESC")]

    def jobs_with_status(self, statuses):
        marks = ",".join("?" * len(statuses))
        return [_job(r) for r in self.db.query(f"SELECT * FROM jobs WHERE status IN ({marks})", tuple(statuses))]

    def update_job(self, job_id, **fields):
        if "options" in fields:
            fields["options"] = dumps(fields["options"])
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.db.tx() as c:
            c.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id))

    def transition_job(self, job_id, from_statuses, to_status, **fields):
        """Atomic compare-and-set on job status. Returns False when the job was not in `from_statuses`."""
        fields["status"] = to_status
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        marks = ",".join("?" * len(from_statuses))
        with self.db.tx() as c:
            cur = c.execute(f"UPDATE jobs SET {assignments} WHERE id = ? AND status IN ({marks})",
                            (*fields.values(), job_id, *from_statuses))
            return cur.rowcount == 1

    def delete_job(self, job_id):
        with self.db.tx() as c:
            c.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
            c.execute("DELETE FROM items WHERE job_id = ?", (job_id,))
            c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def job_counts(self, job_id):
        counts = {"total": 0, "excluded": 0, "unplanned": 0, "errors": 0, "warnings": 0,
                  "ops": {}, "status": {s: 0 for s in ITEM_STATUSES}}
        for row in self.db.query(
                "SELECT included, op, status, COUNT(*) AS n, "
                "SUM(CASE WHEN messages LIKE '%\"level\":\"warning\"%' THEN 1 ELSE 0 END) AS warn "
                "FROM items WHERE job_id = ? GROUP BY included, op, status", (job_id,)):
            n = row["n"]
            counts["total"] += n
            if not row["included"]:
                counts["excluded"] += n
                continue
            counts["warnings"] += row["warn"] or 0
            if row["op"] is None:
                counts["unplanned"] += n
            elif row["op"] == "error":
                counts["errors"] += n
            counts["ops"][row["op"] or "unplanned"] = counts["ops"].get(row["op"] or "unplanned", 0) + n
            counts["status"][row["status"]] = counts["status"].get(row["status"], 0) + n
        included = counts["total"] - counts["excluded"]
        done = counts["status"]["succeeded"] + counts["status"]["skipped"] + counts["status"]["failed"]
        counts["included"] = included
        counts["done"] = done
        counts["remaining"] = counts["status"]["pending"] + counts["status"]["running"]
        return counts

    def plan_fingerprint(self, job_id):
        """Hash of everything that will be sent to the API. Approval is bound to this value, so any edit,
        exclusion or re-plan after approving automatically requires a new approval."""
        job = self.get_job(job_id)
        rows = self.db.query("SELECT id, revision, op, payload FROM items WHERE job_id = ? AND included = 1 "
                             "ORDER BY id", (job_id,))
        return fingerprint({
            "connection": job["connection_id"],
            "action": job["action"],
            "options": job["options"],
            "items": [[r["id"], r["revision"], r["op"], r["payload"]] for r in rows],
        })

    def is_approved(self, job_id):
        job = self.get_job(job_id)
        return bool(job["approved_hash"]) and job["approved_hash"] == self.plan_fingerprint(job_id)

    # ---------------------------------------------------------------- items
    ITEM_FILTERS = {
        "all": "",
        "errors": "AND included = 1 AND op = 'error'",
        "warnings": "AND included = 1 AND messages LIKE '%\"level\":\"warning\"%'",
        "excluded": "AND included = 0",
        "pending": "AND included = 1 AND status = 'pending'",
        "succeeded": "AND included = 1 AND status = 'succeeded'",
        "skipped": "AND included = 1 AND status = 'skipped'",
        "failed": "AND included = 1 AND status = 'failed'",
        "create": "AND included = 1 AND op = 'create'",
        "delete": "AND included = 1 AND op = 'delete'",
        "skip": "AND included = 1 AND op = 'skip'",
    }

    def list_items(self, job_id, filter_name="all", q="", offset=0, limit=100):
        where = self.ITEM_FILTERS.get(filter_name, "")
        params = [job_id]
        if q:
            where += " AND (input LIKE ? OR target LIKE ? OR remote_id LIKE ?)"
            like = f"%{q}%"
            params += [like, like, like]
        total = self.db.query_one(f"SELECT COUNT(*) FROM items WHERE job_id = ? {where}", tuple(params))[0]
        rows = self.db.query(f"SELECT * FROM items WHERE job_id = ? {where} ORDER BY row_no LIMIT ? OFFSET ?",
                             (*params, limit, offset))
        return total, [_item(r) for r in rows]

    def all_items(self, job_id):
        return [_item(r) for r in self.db.query("SELECT * FROM items WHERE job_id = ? ORDER BY row_no", (job_id,))]

    def get_item(self, job_id, item_id):
        item = _item(self.db.query_one("SELECT * FROM items WHERE job_id = ? AND id = ?", (job_id, item_id)))
        if item is None:
            raise NotFound(f"Item {item_id} not found")
        return item

    def set_item_plan(self, item_id, op, target, payload, messages):
        with self.db.tx() as c:
            c.execute("UPDATE items SET op = ?, target = ?, payload = ?, messages = ?, updated_at = ? WHERE id = ?",
                      (op, target, dumps(payload) if payload is not None else None, dumps(messages),
                       now_iso(), item_id))

    def set_item_messages(self, item_id, messages, op=None):
        with self.db.tx() as c:
            if op is None:
                c.execute("UPDATE items SET messages = ? WHERE id = ?", (dumps(messages), item_id))
            else:
                c.execute("UPDATE items SET messages = ?, op = ? WHERE id = ?", (dumps(messages), op, item_id))

    def update_item_input(self, item_id, new_input):
        """Stores an edited row. Bumps the revision (which invalidates approval) and clears the old plan.
        A failed item that is edited becomes pending again so it can be retried."""
        with self.db.tx() as c:
            c.execute("UPDATE items SET input = ?, revision = revision + 1, op = NULL, payload = NULL, "
                      "status = CASE WHEN status = 'failed' THEN 'pending' ELSE status END, error = NULL, "
                      "updated_at = ? WHERE id = ?", (dumps(new_input), now_iso(), item_id))

    def set_included(self, job_id, item_ids, included):
        marks = ",".join("?" * len(item_ids))
        with self.db.tx() as c:
            cur = c.execute(f"UPDATE items SET included = ?, updated_at = ? WHERE job_id = ? AND id IN ({marks}) "
                            f"AND status IN ('pending', 'failed')", (int(included), now_iso(), job_id, *item_ids))
            return cur.rowcount

    def item_ids(self, job_id, filter_name):
        where = self.ITEM_FILTERS[filter_name]
        return [r[0] for r in self.db.query(f"SELECT id FROM items WHERE job_id = ? {where}", (job_id,))]

    def claim_next_item(self, job_id):
        """Atomically picks the next runnable item and marks it running."""
        ops = ",".join(f"'{op}'" for op in EXECUTABLE_OPS)
        with self.db.tx() as c:
            row = c.execute(f"SELECT id FROM items WHERE job_id = ? AND included = 1 AND status = 'pending' "
                            f"AND op IN ({ops}) ORDER BY row_no LIMIT 1", (job_id,)).fetchone()
            if row is None:
                return None
            c.execute("UPDATE items SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE id = ?",
                      (now_iso(), row["id"]))
        return self.get_item(job_id, row["id"])

    def finish_item(self, item_id, status, remote_id=None, result=None, error=None):
        with self.db.tx() as c:
            c.execute("UPDATE items SET status = ?, remote_id = COALESCE(?, remote_id), result = ?, error = ?, "
                      "updated_at = ? WHERE id = ?",
                      (status, remote_id, dumps(result) if result is not None else None, error, now_iso(), item_id))

    def release_item(self, item_id):
        """Puts an interrupted item back in the queue, marked in doubt so the next run reconciles it."""
        with self.db.tx() as c:
            c.execute("UPDATE items SET status = 'pending', result = ?, updated_at = ? WHERE id = ? "
                      "AND status = 'running'", (IN_DOUBT, now_iso(), item_id))

    def release_running_items(self, job_id):
        with self.db.tx() as c:
            return c.execute("UPDATE items SET status = 'pending', result = ?, updated_at = ? WHERE job_id = ? "
                             "AND status = 'running'", (IN_DOUBT, now_iso(), job_id)).rowcount

    def reset_failed_items(self, job_id):
        with self.db.tx() as c:
            return c.execute("UPDATE items SET status = 'pending', error = NULL, updated_at = ? WHERE job_id = ? "
                             "AND included = 1 AND status = 'failed'", (now_iso(), job_id)).rowcount

    # ---------------------------------------------------------------- audit log
    def add_event(self, job_id, level, kind, message, item_id=None, data=None):
        with self.db.tx() as c:
            c.execute("INSERT INTO events (job_id, item_id, ts, level, kind, message, data) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (job_id, item_id, now_iso(), level, kind, message, dumps(data) if data is not None else None))

    def list_events(self, job_id, after_id=0, limit=200):
        rows = self.db.query("SELECT * FROM events WHERE job_id = ? AND id > ? ORDER BY id DESC LIMIT ?",
                             (job_id, after_id, limit))
        events = []
        for r in rows:
            event = dict(r)
            event["data"] = loads(event["data"])
            events.append(event)
        return events
