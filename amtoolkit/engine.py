"""Background execution: planning and running jobs, with pause/resume/cancel and crash recovery.

One worker thread per active job. Items run one at a time, in CSV order. All progress is written to SQLite
as it happens, so closing the browser or even killing the process never loses track of what was done.
"""
import logging
import threading
import time

from .actions import ItemFailed, PlanContext, get_action, msg
from .connectors import AuthError, ConfigError, ConnectorError, Interrupted
from .store import EDITABLE_ITEM_STATUSES, Store
from .util import now_iso

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 5


class JobStateError(Exception):
    """The requested operation is not allowed in the job's current state."""


def plan_item(store: Store, action, ctx: PlanContext, item, options):
    """Plans one row and stores the result. Raises AuthError/ConfigError, which abort the whole plan."""
    try:
        plan = action.plan(ctx, item["input"], options)
    except (AuthError, ConfigError):
        raise
    except ConnectorError as exc:
        plan_op, plan_messages = "error", [msg("error", f"Lookup failed: {exc}", None, "lookup_failed")]
        store.set_item_plan(item["id"], plan_op, "", None, plan_messages)
        return
    store.set_item_plan(item["id"], plan.op, plan.target, plan.payload, plan.messages)


def recheck_duplicates(store: Store, action, job_id):
    """Flags included rows of one job that target the same object. Re-run after every edit and
    include/exclude. Returns the ids of rows that stopped being duplicates and need a fresh plan."""
    items = store.all_items(job_id)
    groups = {}
    for item in items:
        key = action.duplicate_key(item["input"]) if item["included"] else None
        if key:
            groups.setdefault(key, []).append(item)
    dupes = {i["id"]: rows for rows in groups.values() if len(rows) > 1 for i in rows}
    replan = []
    for item in items:
        messages = [m for m in item["messages"] if m.get("code") != "duplicate"]
        op = item["op"]
        if item["id"] in dupes:
            lines = ", ".join(str(o["row_no"]) for o in dupes[item["id"]] if o["id"] != item["id"])
            messages.append(msg("error", f"Duplicate of line {lines} in this file", None, "duplicate"))
            if op is not None:
                op = "error"
        elif (op == "error" and len(messages) < len(item["messages"])
              and not any(m["level"] == "error" for m in messages)):
            replan.append(item["id"])
        if messages != item["messages"] or op != item["op"]:
            store.set_item_messages(item["id"], messages, op)
    return replan


class Runner(threading.Thread):
    def __init__(self, engine, job_id, mode, only_unplanned=False):
        super().__init__(name=f"{mode}-{job_id}", daemon=True)
        self.engine = engine
        self.store = engine.store
        self.job_id = job_id
        self.mode = mode
        self.only_unplanned = only_unplanned
        self.abort = threading.Event()
        self.reason = None  # "pause" | "cancel"

    def stop(self, reason):
        self.reason = reason
        self.abort.set()

    def run(self):
        try:
            if self.mode == "plan":
                self._plan()
            else:
                self._execute()
        except Exception as exc:  # last-resort guard: never leave a job stuck in an active state
            log.exception("Runner for %s crashed", self.job_id)
            self.store.release_running_items(self.job_id)
            target = "review" if self.mode == "plan" else "paused"
            self.store.transition_job(self.job_id, ("planning", "running", "pausing"), target,
                                      error=f"Unexpected error: {exc}")
            self.store.add_event(self.job_id, "error", "crash", f"Unexpected error: {exc}")
        finally:
            self.engine._finished(self.job_id, self)

    # ---------------------------------------------------------------- planning
    def _plan(self):
        job = self.store.get_job(self.job_id)
        action = get_action(job["action"])
        ctx = PlanContext(self.engine.connections.build(job["connection_id"], abort_event=self.abort))
        items = [i for i in self.store.all_items(self.job_id)
                 if i["status"] in EDITABLE_ITEM_STATUSES and (not self.only_unplanned or i["op"] is None)]
        self.store.add_event(self.job_id, "info", "plan_started", f"Planning {len(items)} row(s) (read-only lookups)")
        error = None
        done = 0
        try:
            for item in items:
                if self.abort.is_set():
                    break
                plan_item(self.store, action, ctx, item, job["options"])
                done += 1
        except Interrupted:
            pass
        except (AuthError, ConfigError) as exc:
            error = str(exc)
        recheck_duplicates(self.store, action, self.job_id)
        if self.reason == "cancel":
            self.store.transition_job(self.job_id, ("planning",), "cancelled", finished_at=now_iso())
            self.store.add_event(self.job_id, "warning", "cancelled", "Cancelled during planning")
            return
        self.store.transition_job(self.job_id, ("planning",), "review", error=error)
        if error:
            self.store.add_event(self.job_id, "error", "plan_failed", f"Planning stopped: {error}")
        else:
            counts = self.store.job_counts(self.job_id)
            summary = ", ".join(f"{n} {op}" for op, n in sorted(counts["ops"].items()))
            self.store.add_event(self.job_id, "info", "plan_finished",
                                 f"Planned {done} row(s): {summary or 'nothing'}", data=counts["ops"])

    # ---------------------------------------------------------------- execution
    def _execute(self):
        job = self.store.get_job(self.job_id)
        action = get_action(job["action"])
        options = job["options"]
        ctx = PlanContext(self.engine.connections.build(job["connection_id"], abort_event=self.abort))
        delay = max(0.0, float(options.get("delay_ms") or 0) / 1000)
        consecutive = 0
        finished = False
        stop_error = None
        while not self.abort.is_set():
            item = self.store.claim_next_item(self.job_id)
            if item is None:
                finished = True
                break
            label = item["target"] or f"line {item['row_no']}"
            try:
                if item["op"] == "skip":
                    self.store.finish_item(item["id"], "skipped", result={"message": "Skipped by plan"})
                    continue
                outcome = action.execute(ctx, item, options)
            except Interrupted:
                self.store.release_item(item["id"])
                break
            except (AuthError, ConfigError) as exc:
                self.store.release_item(item["id"])
                stop_error = str(exc)
                break
            except (ItemFailed, ConnectorError) as exc:
                in_doubt = getattr(exc, "in_doubt", False) or getattr(exc, "ambiguous", False)
                self.store.finish_item(item["id"], "failed", error=str(exc),
                                       result={"in_doubt": True} if in_doubt else None)
                self.store.add_event(self.job_id, "error", "item_failed", f"{label}: {exc}", item["id"])
                consecutive += 1
            except Exception as exc:
                log.exception("Item %s failed unexpectedly", item["id"])
                self.store.finish_item(item["id"], "failed", error=f"Unexpected error: {exc}",
                                       result={"in_doubt": True})
                self.store.add_event(self.job_id, "error", "item_failed", f"{label}: unexpected error {exc}",
                                     item["id"])
                consecutive += 1
            else:
                result = dict(outcome.result or {}, message=outcome.message)
                self.store.finish_item(item["id"], outcome.status, outcome.remote_id, result)
                self.store.add_event(self.job_id, "info", f"item_{outcome.status}",
                                     f"{label}: {outcome.message}", item["id"], {"remote_id": outcome.remote_id})
                consecutive = 0
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                stop_error = (f"{consecutive} failures in a row — paused automatically. Check the connection and "
                              f"the errors below, then resume.")
                break
            if delay and self.abort.wait(delay):
                break

        if finished:
            counts = self.store.job_counts(self.job_id)
            s = counts["status"]
            self.store.transition_job(self.job_id, ("running", "pausing"), "completed", finished_at=now_iso())
            self.store.add_event(self.job_id, "info" if not s["failed"] else "warning", "completed",
                                 f"Finished: {s['succeeded']} succeeded, {s['skipped']} skipped, "
                                 f"{s['failed']} failed")
        elif self.reason == "cancel":
            self.store.transition_job(self.job_id, ("running", "pausing"), "cancelled", finished_at=now_iso())
            self.store.add_event(self.job_id, "warning", "cancelled", "Cancelled; remaining rows were not run")
        else:
            self.store.transition_job(self.job_id, ("running", "pausing"), "paused", error=stop_error)
            if stop_error:
                self.store.add_event(self.job_id, "error", "auto_paused", stop_error)
            else:
                self.store.add_event(self.job_id, "info", "paused", "Paused")


class Engine:
    def __init__(self, store: Store, connections):
        self.store = store
        self.connections = connections
        self._runners = {}
        self._lock = threading.Lock()

    def recover(self):
        """Called at startup. Anything that was active when the process died is made safe to resume."""
        for job in self.store.jobs_with_status(("planning", "running", "pausing")):
            released = self.store.release_running_items(job["id"])
            if job["status"] == "planning":
                self.store.transition_job(job["id"], ("planning",), "review",
                                          error="Planning was interrupted; run the plan again")
            else:
                self.store.transition_job(job["id"], ("running", "pausing"), "paused",
                                          error="The toolkit was stopped while this job was running")
            self.store.add_event(job["id"], "warning", "recovered",
                                 f"Recovered after restart; {released} in-flight row(s) will be re-checked "
                                 "before being retried")

    def is_active(self, job_id):
        with self._lock:
            return job_id in self._runners

    def _launch(self, job_id, mode, **kwargs):
        with self._lock:
            if job_id in self._runners:
                raise JobStateError("This job is already busy")
            runner = Runner(self, job_id, mode, **kwargs)
            self._runners[job_id] = runner
        runner.start()
        return runner

    def _finished(self, job_id, runner):
        with self._lock:
            if self._runners.get(job_id) is runner:
                del self._runners[job_id]

    def plan(self, job_id, only_unplanned=False):
        if not self.store.transition_job(job_id, ("planning", "review", "approved", "paused"), "planning",
                                         approved_hash=None, approved_at=None, error=None):
            raise JobStateError("The plan can only be refreshed before or between runs")
        return self._launch(job_id, "plan", only_unplanned=only_unplanned)

    def start(self, job_id):
        if not self.store.is_approved(job_id):
            raise JobStateError("Approve the current plan before running it")
        if not self.store.transition_job(job_id, ("approved", "paused"), "running", error=None):
            raise JobStateError("Only approved or paused jobs can be started")
        job = self.store.get_job(job_id)
        if not job["started_at"]:
            self.store.update_job(job_id, started_at=now_iso())
        self.store.add_event(job_id, "info", "started", "Run started")
        return self._launch(job_id, "execute")

    def pause(self, job_id):
        with self._lock:
            runner = self._runners.get(job_id)
        if runner is None or runner.mode != "execute":
            raise JobStateError("This job is not running")
        self.store.transition_job(job_id, ("running",), "pausing")
        self.store.add_event(job_id, "info", "pause_requested", "Pause requested; finishing the current row")
        runner.stop("pause")

    def cancel(self, job_id):
        with self._lock:
            runner = self._runners.get(job_id)
        if runner is not None:
            runner.stop("cancel")
            self.store.add_event(job_id, "warning", "cancel_requested", "Cancel requested")
            return
        if not self.store.transition_job(job_id, ("review", "approved", "paused"), "cancelled",
                                         finished_at=now_iso()):
            raise JobStateError("This job cannot be cancelled now")
        self.store.add_event(job_id, "warning", "cancelled", "Cancelled; remaining rows will not be run")

    def shutdown(self, timeout=15):
        """Pauses every running job and waits for the current rows to finish."""
        with self._lock:
            runners = list(self._runners.values())
        for runner in runners:
            if runner.mode == "execute":
                self.store.transition_job(runner.job_id, ("running",), "pausing")
            runner.stop("pause")
        deadline = time.monotonic() + timeout
        for runner in runners:
            runner.join(max(0.1, deadline - time.monotonic()))
