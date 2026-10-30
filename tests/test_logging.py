"""Backend logging: audit events, API requests and connector calls reach the log and the Logs page."""
import logging

from amtoolkit import logs
from tests.test_workflow import make_job


def test_job_events_requests_and_connector_calls_are_logged(client, toolkit, sandbox):
    root = logging.getLogger()
    previous = root.level
    root.addHandler(logs.memory)
    root.setLevel(logging.DEBUG)
    try:
        start = max([r["id"] for r in logs.memory.since()] or [0])
        job_id = make_job(toolkit, sandbox)
        client.post(f"/api/connections/{sandbox['id']}/test")
        entries = logs.memory.since(start)
        text = [f"{e['logger']} {e['message']}" for e in entries]
        assert any(t.startswith(f"amtoolkit.audit job {job_id} [plan_finished]") for t in text)
        assert any("find_many('identity'" in t for t in text)
        assert any("prefetched" in t for t in text)
        assert any(t.startswith("amtoolkit.app POST /api/connections/") and "-> 200" in t for t in text)
        assert all(e["level"] in ("WARNING", "ERROR") for e in logs.memory.since(start, level="WARNING"))

        rows = client.get(f"/api/logs?after={start}&level=INFO").get_json()
        assert rows and all(r["level"] != "DEBUG" for r in rows)
    finally:
        root.removeHandler(logs.memory)
        root.setLevel(previous)


def test_setup_writes_a_log_file(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers), root.level, getattr(root, "_amtoolkit", False)
    try:
        root._amtoolkit = False
        path = logs.setup(tmp_path, "INFO")
        logging.getLogger("amtoolkit.test").info("hello from the test")
        for handler in root.handlers:
            handler.flush()
        assert "hello from the test" in path.read_text("utf-8")
    finally:
        for handler in root.handlers[:]:
            if handler not in before[0]:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(before[1])
        root._amtoolkit = before[2]
