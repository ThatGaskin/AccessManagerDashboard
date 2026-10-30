import threading

import pytest

from amtoolkit.engine import JobStateError
from tests.conftest import wait_for

CSV = (b"name,description,owner,source,entitlements,approval_schemes\n"
       b"AP One,first,alice@example.com,Active Directory,Finance-Read,manager\n"
       b"AP Two,second,bob,Salesforce,Standard User;Read Only,owner\n"
       b"AP Three,third,carol,Workday,Employee,\n")


def make_job(toolkit, conn, data=CSV, **kw):
    job_id = toolkit.create_job(name="test", action_key="access_profile.create", connection_id=conn["id"],
                                options=kw, filename="t.csv", data=data)
    wait_for(lambda: toolkit.store.get_job(job_id)["status"] == "review")
    return job_id


def approve(toolkit, job_id):
    toolkit.approve(job_id, toolkit.store.plan_fingerprint(job_id))


def run_to_end(toolkit, job_id):
    toolkit.engine.start(job_id)
    wait_for(lambda: toolkit.store.get_job(job_id)["status"] in ("completed", "paused"))


def test_plan_resolves_references(toolkit, sandbox):
    job_id = make_job(toolkit, sandbox)
    items = toolkit.store.all_items(job_id)
    assert [i["op"] for i in items] == ["create", "create", "create"]
    p = items[1]["payload"]
    assert p["owner"]["id"] == "2c9180835d2e5168015d32f890ca1582"
    assert [e["name"] for e in p["entitlements"]] == ["Standard User", "Read Only"]
    assert p["accessRequestConfig"]["approvalSchemes"] == [{"approverType": "OWNER", "approverId": None}]


def test_errors_block_approval_until_fixed_or_excluded(toolkit, sandbox):
    data = CSV + b"Bad,x,nobody@example.com,Active Directory,Finance-Read,manager\n"
    job_id = make_job(toolkit, sandbox, data)
    view = toolkit.job_view(job_id)
    assert view["counts"]["errors"] == 1 and not view["allowed"]["approve"]
    with pytest.raises(JobStateError):
        approve(toolkit, job_id)
    bad = [i for i in toolkit.store.all_items(job_id) if i["op"] == "error"][0]
    fixed = toolkit.edit_item(job_id, bad["id"], dict(bad["input"], owner="alice"))
    assert fixed["op"] == "create" and fixed["revision"] == 1
    approve(toolkit, job_id)
    assert toolkit.store.get_job(job_id)["status"] == "approved"


def test_duplicates_are_flagged_and_cleared(toolkit, sandbox):
    data = CSV + b"AP One,dupe,alice,Active Directory,Finance-Read,manager\n"
    job_id = make_job(toolkit, sandbox, data)
    items = toolkit.store.all_items(job_id)
    assert [i["op"] for i in items] == ["error", "create", "create", "error"]
    toolkit.set_included(job_id, [items[3]["id"]], False)
    assert toolkit.store.get_item(job_id, items[0]["id"])["op"] == "create"


def test_edit_after_approval_revokes_it(toolkit, sandbox):
    job_id = make_job(toolkit, sandbox)
    approve(toolkit, job_id)
    item = toolkit.store.all_items(job_id)[0]
    toolkit.edit_item(job_id, item["id"], dict(item["input"], description="changed"))
    job = toolkit.job_view(job_id)
    assert job["status"] == "review" and not job["approved"]
    with pytest.raises(JobStateError):
        toolkit.engine.start(job_id)


def test_stale_fingerprint_is_rejected(toolkit, sandbox):
    job_id = make_job(toolkit, sandbox)
    seen = toolkit.store.plan_fingerprint(job_id)
    item = toolkit.store.all_items(job_id)[0]
    toolkit.edit_item(job_id, item["id"], dict(item["input"], description="changed elsewhere"))
    with pytest.raises(JobStateError, match="changed since you reviewed"):
        toolkit.approve(job_id, seen)


def test_production_needs_typed_confirmation(toolkit):
    prod = toolkit.save_connection({"name": "Prod Sandbox", "kind": "mock", "environment": "production",
                                    "config": {"latency_ms": "0", "failure_rate": "0"}})
    job_id = make_job(toolkit, prod)
    fp = toolkit.store.plan_fingerprint(job_id)
    with pytest.raises(JobStateError, match="Type the connection name"):
        toolkit.approve(job_id, fp, "yes")
    toolkit.approve(job_id, fp, "Prod Sandbox")


def test_run_complete_and_rerun_is_idempotent(toolkit, sandbox):
    job_id = make_job(toolkit, sandbox)
    approve(toolkit, job_id)
    run_to_end(toolkit, job_id)
    counts = toolkit.store.job_counts(job_id)
    assert counts["status"]["succeeded"] == 3
    # The same file again: everything already exists and is planned as skip.
    job2 = make_job(toolkit, sandbox)
    assert {i["op"] for i in toolkit.store.all_items(job2)} == {"skip"}


def test_pause_resume_and_edit_while_paused(toolkit):
    slow = toolkit.save_connection({"name": "Slow", "kind": "mock", "environment": "sandbox",
                                    "config": {"latency_ms": "40", "failure_rate": "0"}})
    rows = b"name,owner,source,entitlements\n" + b"".join(
        f"P{n},alice,Active Directory,Finance-Read\n".encode() for n in range(25))
    job_id = make_job(toolkit, slow, rows)
    approve(toolkit, job_id)
    toolkit.engine.start(job_id)
    wait_for(lambda: toolkit.store.job_counts(job_id)["status"]["succeeded"] >= 2)
    toolkit.engine.pause(job_id)
    wait_for(lambda: toolkit.store.get_job(job_id)["status"] == "paused")
    done = toolkit.store.job_counts(job_id)["status"]["succeeded"]
    assert 2 <= done < 25
    # Ran rows are locked; pending rows can be edited, which requires re-approval before resuming.
    first, last = toolkit.store.all_items(job_id)[0], toolkit.store.all_items(job_id)[-1]
    with pytest.raises(JobStateError):
        toolkit.edit_item(job_id, first["id"], dict(first["input"], name="nope"))
    toolkit.edit_item(job_id, last["id"], dict(last["input"], name="P-last-renamed"))
    with pytest.raises(JobStateError):
        toolkit.engine.start(job_id)
    approve(toolkit, job_id)
    run_to_end(toolkit, job_id)
    assert toolkit.store.job_counts(job_id)["status"]["succeeded"] == 25
    assert toolkit.store.get_item(job_id, last["id"])["payload"]["name"] == "P-last-renamed"


def test_crash_recovery_reconciles_in_flight_row(toolkit, sandbox, tmp_path):
    job_id = make_job(toolkit, sandbox)
    approve(toolkit, job_id)
    # Simulate: row 1 was sent to the API, then the process died before recording the result.
    item = toolkit.store.claim_next_item(job_id)
    toolkit.connections.build(sandbox["id"]).create_access_profile({"name": "AP One", "source": {"name": "x"}})
    toolkit.store.update_job(job_id, status="running")
    toolkit.engine.recover()
    job = toolkit.store.get_job(job_id)
    assert job["status"] == "paused"
    approve_needed = toolkit.store.is_approved(job_id)
    assert approve_needed  # recovery does not change the plan
    run_to_end(toolkit, job_id)
    recovered = toolkit.store.get_item(job_id, item["id"])
    assert recovered["status"] == "succeeded" and recovered["result"]["reconciled"]


def test_existing_profile_created_by_someone_else_is_skipped_not_claimed(toolkit, sandbox):
    job_id = make_job(toolkit, sandbox)
    approve(toolkit, job_id)
    toolkit.connections.build(sandbox["id"]).create_access_profile({"name": "AP Two", "source": {"name": "x"}})
    run_to_end(toolkit, job_id)
    item = toolkit.store.all_items(job_id)[1]
    assert item["status"] == "skipped"
    rollback_rows = [i for i in toolkit.store.all_items(job_id) if i["status"] == "succeeded"]
    assert len(rollback_rows) == 2


def test_rollback_job_deletes_only_what_was_created(toolkit, sandbox):
    job_id = make_job(toolkit, sandbox)
    approve(toolkit, job_id)
    run_to_end(toolkit, job_id)
    rb = toolkit.rollback(job_id)
    wait_for(lambda: toolkit.store.get_job(rb)["status"] == "review")
    assert {i["op"] for i in toolkit.store.all_items(rb)} == {"delete"}
    approve(toolkit, rb)
    run_to_end(toolkit, rb)
    assert toolkit.store.job_counts(rb)["status"]["succeeded"] == 3
    assert toolkit.connections.build(sandbox["id"]).find_access_profiles("AP One") == []


def test_failures_auto_pause_and_retry(toolkit):
    flaky = toolkit.save_connection({"name": "Flaky", "kind": "mock", "environment": "sandbox",
                                     "config": {"latency_ms": "0", "failure_rate": "1"}})
    rows = b"name,owner,source,entitlements\n" + b"".join(
        f"F{n},alice,Active Directory,Finance-Read\n".encode() for n in range(8))
    job_id = make_job(toolkit, flaky, rows)
    approve(toolkit, job_id)
    run_to_end(toolkit, job_id)
    job = toolkit.store.get_job(job_id)
    assert job["status"] == "paused" and "failures in a row" in job["error"]
    assert toolkit.store.job_counts(job_id)["status"]["failed"] == 5
    toolkit.store.update_connection(flaky["id"], "Flaky", "sandbox", {"latency_ms": "0", "failure_rate": "0"})
    toolkit.retry_failed(job_id)
    run_to_end(toolkit, job_id)
    assert toolkit.store.job_counts(job_id)["status"]["succeeded"] == 8


def test_cancel_running_job(toolkit):
    slow = toolkit.save_connection({"name": "Slow2", "kind": "mock", "environment": "sandbox",
                                    "config": {"latency_ms": "30", "failure_rate": "0"}})
    rows = b"name,owner,source,entitlements\n" + b"".join(
        f"C{n},alice,Active Directory,Finance-Read\n".encode() for n in range(30))
    job_id = make_job(toolkit, slow, rows)
    approve(toolkit, job_id)
    toolkit.engine.start(job_id)
    wait_for(lambda: toolkit.store.job_counts(job_id)["status"]["succeeded"] >= 1)
    toolkit.engine.cancel(job_id)
    wait_for(lambda: toolkit.store.get_job(job_id)["status"] == "cancelled")
    assert toolkit.store.job_counts(job_id)["status"]["pending"] > 0
    assert not toolkit.job_view(job_id)["allowed"]["start"]
