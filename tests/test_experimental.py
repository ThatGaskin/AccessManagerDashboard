"""Experimental features, exercised end-to-end against the sandbox."""
import pytest

from tests.conftest import wait_for
from tests.test_workflow import approve, run_to_end

ALICE = "2c9180835d2e5168015d32f890ca1581"
BOB = "2c9180835d2e5168015d32f890ca1582"
CAROL = "2c9180835d2e5168015d32f890ca1583"
LEGACY_VPN = "8f1e2d3c4b5a69788796a5b4c3d2e1f0"  # sandbox profile with a departed owner and no description


def job(toolkit, conn, action, csv):
    job_id = toolkit.create_job(name=action, action_key=action, connection_id=conn["id"], options={},
                                filename="x.csv", data=csv.encode())
    wait_for(lambda: toolkit.store.get_job(job_id)["status"] == "review")
    return job_id


def items(toolkit, job_id):
    return toolkit.store.all_items(job_id)


def run(toolkit, job_id):
    approve(toolkit, job_id)
    run_to_end(toolkit, job_id)
    return toolkit.store.job_counts(job_id)["status"]


def rollback(toolkit, job_id):
    rb = toolkit.rollback(job_id)
    wait_for(lambda: toolkit.store.get_job(rb)["status"] == "review")
    return rb


def obj(toolkit, conn, type_, id_):
    return toolkit.connections.build(conn["id"]).get_object(type_, id_)


def test_actions_and_reports_are_marked_experimental(client):
    meta = client.get("/api/meta").get_json()
    flags = {a["key"]: a["experimental"] for a in meta["actions"]}
    assert flags["access_profile.create"] is False and flags["ownership.reassign"] is True
    assert meta["reports"] and all(r["experimental"] for r in meta["reports"])


def test_update_access_profile_with_before_after_and_rollback(toolkit, sandbox):
    j = job(toolkit, sandbox, "access_profile.update",
            "access_profile,description,owner,approval_schemes,add_entitlements\n"
            "Legacy VPN,Remote access,carol,manager;governance_group:Finance Approvers,Domain Users\n")
    it = items(toolkit, j)[0]
    assert it["op"] == "update"
    assert set(it["payload"]["after"]) == {"description", "owner", "approval_schemes", "entitlements"}
    assert it["payload"]["before"]["owner"] == "2c9180835d2e5168015d32f8900dead0"
    assert any("→" in m["text"] for m in it["messages"])
    assert run(toolkit, j)["succeeded"] == 1
    after = obj(toolkit, sandbox, "access_profile", LEGACY_VPN)
    assert after["owner"] == CAROL and after["description"] == "Remote access" and len(after["entitlements"]) == 2

    rb = rollback(toolkit, j)
    # The previous owner has left, so it cannot be restored: the rollback row says so and can be edited.
    rb_item = items(toolkit, rb)[0]
    assert rb_item["op"] == "error" and rb_item["messages"][0]["field"] == "owner"
    toolkit.edit_item(rb, rb_item["id"], dict(rb_item["input"], owner=""))
    assert items(toolkit, rb)[0]["op"] == "update"
    assert run(toolkit, rb)["succeeded"] == 1
    restored = obj(toolkit, sandbox, "access_profile", LEGACY_VPN)
    assert restored["description"] == "" and restored["approval_schemes"] == [] and len(restored["entitlements"]) == 1


def test_update_refuses_when_object_changed_after_planning(toolkit, sandbox):
    j = job(toolkit, sandbox, "access_profile.update", "access_profile,description\nLegacy VPN,Planned text\n")
    approve(toolkit, j)
    toolkit.connections.build(sandbox["id"]).patch_object("access_profile", LEGACY_VPN, {"description": "Edited"})
    run_to_end(toolkit, j)
    it = items(toolkit, j)[0]
    assert it["status"] == "failed" and "Changed by someone else" in it["error"]


def test_update_with_nothing_to_change(toolkit, sandbox):
    it = items(toolkit, job(toolkit, sandbox, "access_profile.update", "access_profile,description\nLegacy VPN,\n"))[0]
    assert it["op"] == "error" and it["messages"][0]["code"] == "empty"


def test_reassign_ownership_across_types_with_safety_check(toolkit, sandbox):
    csv = ("object_type,object,source,new_owner,current_owner\n"
           "access_profile,Legacy VPN,,bob,\n"
           "source,Workday,,bob,\n"
           "entitlement,Finance-Read,Active Directory,bob,alice\n"
           "governance_group,Finance Approvers,,bob,carol\n"  # wrong current owner → error
           "role,Nope,,bob,\n")
    j = job(toolkit, sandbox, "ownership.reassign", csv)
    ops = [i["op"] for i in items(toolkit, j)]
    assert ops == ["update", "update", "update", "error", "error"]
    assert items(toolkit, j)[3]["messages"][0]["code"] == "owner_mismatch"
    toolkit.bulk(j, "exclude_errors")
    assert run(toolkit, j)["succeeded"] == 3
    assert obj(toolkit, sandbox, "source", "2c91808568c529c60168cca6f90c1302")["owner"] == BOB
    rb = rollback(toolkit, j)
    # Two objects were owned by a departed identity; ownership cannot be handed back to them.
    assert [i["op"] for i in items(toolkit, rb)] == ["error", "error", "update"]
    toolkit.bulk(rb, "exclude_errors")
    assert run(toolkit, rb)["succeeded"] == 1
    ent = items(toolkit, j)[2]["payload"]["object"]["id"]
    assert obj(toolkit, sandbox, "entitlement", ent)["owner"] == ALICE


def test_owner_cannot_be_a_group(toolkit, sandbox):
    it = items(toolkit, job(toolkit, sandbox, "ownership.reassign",
                            "object_type,object,new_owner\naccess_profile,Legacy VPN,Finance Approvers\n"))[0]
    assert it["op"] == "error" and any(m["code"] == "owner_is_group" for m in it["messages"])


def test_update_entitlements_and_rollback(toolkit, sandbox):
    j = job(toolkit, sandbox, "entitlement.update",
            "source,entitlement,description,owner,privileged\nActive Directory,IT-Admins,Domain admins,alice,yes\n")
    it = items(toolkit, j)[0]
    assert set(it["payload"]["after"]) == {"description", "owner"}  # privileged already yes
    assert run(toolkit, j)["succeeded"] == 1
    rb = rollback(toolkit, j)
    assert run(toolkit, rb)["succeeded"] == 1
    ent = obj(toolkit, sandbox, "entitlement", it["payload"]["object"]["id"])
    assert ent["description"] == "" and ent["owner"] == ALICE  # owner had none before: cannot be "un-set"


def test_group_members_add_remove_and_rollback(toolkit, sandbox):
    j = job(toolkit, sandbox, "governance_group.members",
            "group,add,remove\nFinance Approvers,bob;alice,carol\n")
    it = items(toolkit, j)[0]
    assert [x["id"] for x in it["payload"]["add"]] == [BOB]  # alice is already a member
    assert run(toolkit, j)["succeeded"] == 1
    connector = toolkit.connections.build(sandbox["id"])
    gid = it["payload"]["group"]["id"]
    assert {m["id"] for m in connector.list_governance_group_members(gid)} == {ALICE, BOB}
    rb = rollback(toolkit, j)
    assert run(toolkit, rb)["succeeded"] == 1
    assert {m["id"] for m in connector.list_governance_group_members(gid)} == {ALICE, CAROL}


def test_create_role_from_access_profiles_and_rollback(toolkit, sandbox):
    j = job(toolkit, sandbox, "role.create",
            "name,owner,access_profiles\nVPN Role,alice,Legacy VPN\nBad Role,alice,Does Not Exist\n")
    assert [i["op"] for i in items(toolkit, j)] == ["create", "error"]
    toolkit.bulk(j, "exclude_errors")
    assert run(toolkit, j)["succeeded"] == 1
    assert toolkit.connections.build(sandbox["id"]).find_objects("role", "VPN Role")
    rb = rollback(toolkit, j)
    assert run(toolkit, rb)["succeeded"] == 1
    assert toolkit.connections.build(sandbox["id"]).find_objects("role", "VPN Role") == []


def test_access_requests_submit_skip_pending_cancel_and_rollback(toolkit, sandbox):
    csv = ("requested_for,access_type,access,request_type,comment\n"
           "bob,access_profile,Legacy VPN,grant,Project work\n")
    j = job(toolkit, sandbox, "access_request.submit", csv)
    assert items(toolkit, j)[0]["op"] == "request"
    assert run(toolkit, j)["succeeded"] == 1
    # The same request again is skipped because one is already pending.
    assert items(toolkit, job(toolkit, sandbox, "access_request.submit", csv))[0]["op"] == "skip"
    pending = toolkit.connections.build(sandbox["id"]).find_access_requests(BOB)
    assert len(pending) == 1
    c = job(toolkit, sandbox, "access_request.cancel", f"request_id,comment\n{pending[0]['id']},Mistake\n")
    assert run(toolkit, c)["succeeded"] == 1
    rb = rollback(toolkit, j)
    revoke = items(toolkit, rb)[0]
    assert revoke["op"] == "request" and revoke["payload"]["requestType"] == "REVOKE_ACCESS"


def test_forward_approvals_and_rollback(toolkit, sandbox):
    approval = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    j = job(toolkit, sandbox, "approval.forward", f"approval_id,new_approver,comment\n{approval},alice,Bob on leave\n")
    assert items(toolkit, j)[0]["op"] == "forward"
    assert run(toolkit, j)["succeeded"] == 1
    connector = toolkit.connections.build(sandbox["id"])
    assert connector.get_pending_approval(approval)["approver_id"] == ALICE
    rb = rollback(toolkit, j)
    assert run(toolkit, rb)["succeeded"] == 1
    assert connector.get_pending_approval(approval)["approver_id"] == BOB


def run_report(toolkit, conn, key, **params):
    rid = toolkit.reports.start(conn["id"], key, params)
    wait_for(lambda: toolkit.reports.get(rid, with_rows=False)["status"] != "running")
    rep = toolkit.reports.get(rid)
    assert rep["status"] == "completed", rep.get("error")
    return rid, rep


@pytest.mark.parametrize("key, params, expected", [
    ("ap_missing_description", {}, {"Legacy VPN"}),
    ("ap_no_approval", {}, {"Legacy VPN"}),
    ("orphaned_owners", {}, {"Legacy VPN", "Workday", "IT-Admins", "Payroll Admin"}),
    ("owned_by", {"identity": "alice@example.com"}, {"Finance Approvers", "Active Directory", "Salesforce"}),
    ("entitlements_missing_metadata", {"source": "Workday"}, {"Payroll Admin"}),
])
def test_reports(toolkit, sandbox, key, params, expected):
    _, rep = run_report(toolkit, sandbox, key, **params)
    names = {r["name"] for r in rep["rows"]}
    assert expected <= names


def test_report_followup_csv_becomes_a_job(toolkit, sandbox):
    rid, _ = run_report(toolkit, sandbox, "orphaned_owners")
    action, text = toolkit.report_followup_csv(rid)
    assert action.key == "ownership.reassign" and text.splitlines()[0].startswith("object_type,object")
    filled = text.replace(",,\r\n", ",bob,\r\n")  # fill new_owner for every row
    j = job(toolkit, sandbox, "ownership.reassign", filled)
    assert {i["op"] for i in items(toolkit, j)} == {"update"}


def test_report_requires_params(toolkit, sandbox):
    with pytest.raises(ValueError):
        toolkit.reports.start(sandbox["id"], "owned_by", {})


def test_report_api(client, toolkit, sandbox):
    rid = client.post("/api/reports", json={"connection_id": sandbox["id"], "report": "empty_groups"}).get_json()["id"]
    wait_for(lambda: client.get(f"/api/reports/{rid}").get_json()["status"] == "completed")
    assert client.get(f"/api/reports/{rid}/export.csv").status_code == 200
    assert client.get(f"/api/reports/{rid}/followup.csv").status_code == 200
    assert client.delete(f"/api/reports/{rid}").status_code == 200


def test_job_from_report_with_defaults(toolkit, sandbox):
    rid, rep = run_report(toolkit, sandbox, "owned_by", identity=ALICE)
    j = toolkit.create_job_from_report(rid, {"new_owner": BOB, "ignored": "x"})
    wait_for(lambda: toolkit.store.get_job(j)["status"] == "review")
    rows = items(toolkit, j)
    assert len(rows) == rep["count"] and {r["input"]["new_owner"] for r in rows} == {BOB}
    assert {r["op"] for r in rows} == {"update"}
    assert toolkit.store.get_job(j)["connection_id"] == sandbox["id"]


def test_job_from_report_without_defaults_leaves_blanks_to_fill(toolkit, sandbox):
    rid, _ = run_report(toolkit, sandbox, "orphaned_owners")
    j = toolkit.create_job_from_report(rid, {})
    wait_for(lambda: toolkit.store.get_job(j)["status"] == "review")
    assert {r["op"] for r in items(toolkit, j)} == {"error"}  # new_owner still to be chosen per row


def test_identity_search_api(client, sandbox):
    found = client.get(f"/api/connections/{sandbox['id']}/identities?q=ali").get_json()
    assert [i["name"] for i in found] == ["Alice Admin"]
    assert client.get(f"/api/connections/{sandbox['id']}/identities?q=a").get_json() == []


def test_update_access_profile_creates_missing_approver_group(toolkit, sandbox):
    j = job(toolkit, sandbox, "access_profile.update",
            "access_profile,approval_schemes,new_group_owner,new_group_members\n"
            "Legacy VPN,governance_group:VPN Approvers,carol,alice;bob\n")
    item = items(toolkit, j)[0]
    assert item["op"] == "update"
    assert item["payload"]["after"]["approval_schemes"] == ["governance_group:new:VPN Approvers"]
    assert run(toolkit, j)["succeeded"] == 1
    connector = toolkit.connections.build(sandbox["id"])
    gid = connector.find_governance_groups("VPN Approvers")[0]["id"]
    assert obj(toolkit, sandbox, "access_profile", LEGACY_VPN)["approval_schemes"] == [f"governance_group:{gid}"]
    assert {m["id"] for m in connector.list_governance_group_members(gid)} == {ALICE, BOB}
