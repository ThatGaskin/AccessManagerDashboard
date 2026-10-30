import pytest

from amtoolkit.connectors.sailpoint import SailPointISC
from amtoolkit.engine import JobStateError
from tests.conftest import wait_for
from tests.test_workflow import approve, make_job, run_to_end

GROUP_ID = "2c9180867a4e5b2a017a4f1d2b3c0001"  # "Finance Approvers" in the sandbox seed


def rows(job_id, toolkit):
    return toolkit.store.all_items(job_id)


def test_approver_group_is_resolved_by_name(toolkit, sandbox):
    csv = (b"name,owner,source,entitlements,approval_schemes\n"
           b"GG AP,alice,Active Directory,Finance-Read,manager;governance_group:finance approvers\n")
    item = rows(make_job(toolkit, sandbox, csv), toolkit)[0]
    assert item["op"] == "create"
    assert item["payload"]["accessRequestConfig"]["approvalSchemes"] == [
        {"approverType": "MANAGER", "approverId": None},
        {"approverType": "GOVERNANCE_GROUP", "approverId": GROUP_ID}]


def test_group_as_owner_is_explained(toolkit, sandbox):
    csv = b"name,owner,source,entitlements\nGG AP,Finance Approvers,Active Directory,Finance-Read\n"
    item = rows(make_job(toolkit, sandbox, csv), toolkit)[0]
    assert item["op"] == "error"
    err = next(m for m in item["messages"] if m["code"] == "owner_is_group")
    assert "owners must be individual identities" in err["text"]


def test_missing_groups_flow(toolkit, sandbox):
    csv = (b"name,owner,source,entitlements,approval_schemes\n"
           b"AP1,alice,Active Directory,Finance-Read,governance_group:HR Approvers\n"
           b"AP2,alice,Active Directory,HR-Read,governance_group:hr approvers;governance_group:IT Approvers\n")
    ap_job = make_job(toolkit, sandbox, csv)
    view = toolkit.job_view(ap_job)
    assert view["missing_governance_groups"] == ["HR Approvers", "IT Approvers"]
    assert view["allowed"]["create_groups"] and not view["allowed"]["approve"]

    with pytest.raises(ValueError):
        toolkit.create_missing_governance_groups(ap_job, owner="")
    gg_job = toolkit.create_missing_governance_groups(ap_job, owner="carol", members="alice;bob")
    wait_for(lambda: toolkit.store.get_job(gg_job)["status"] == "review")
    planned = rows(gg_job, toolkit)
    assert [i["op"] for i in planned] == ["create", "create"]
    assert len(planned[0]["payload"]["members"]) == 2
    assert toolkit.job_view(gg_job)["parent"]["id"] == ap_job

    approve(toolkit, gg_job)
    run_to_end(toolkit, gg_job)
    assert toolkit.store.job_counts(gg_job)["status"]["succeeded"] == 2

    toolkit.replan(ap_job)
    wait_for(lambda: toolkit.store.get_job(ap_job)["status"] == "review")
    assert [i["op"] for i in rows(ap_job, toolkit)] == ["create", "create"]
    assert toolkit.job_view(ap_job)["missing_governance_groups"] == []


def test_group_without_members_warns(toolkit, sandbox):
    item = rows(make_job_gg(toolkit, sandbox, b"name,owner,members\nEmpty Team,alice,\n"), toolkit)[0]
    assert item["op"] == "create"
    assert any(m["level"] == "warning" and m["field"] == "members" for m in item["messages"])


def make_job_gg(toolkit, conn, data):
    job_id = toolkit.create_job(name="gg", action_key="governance_group.create", connection_id=conn["id"],
                                options={}, filename="g.csv", data=data)
    wait_for(lambda: toolkit.store.get_job(job_id)["status"] == "review")
    return job_id


def test_interrupted_group_create_is_reconciled_and_members_added(toolkit, sandbox):
    job_id = make_job_gg(toolkit, sandbox, b"name,owner,members\nOps Team,alice,bob;carol\n")
    approve(toolkit, job_id)
    item = toolkit.store.claim_next_item(job_id)
    connector = toolkit.connections.build(sandbox["id"])
    connector.create_governance_group(item["payload"]["group"])  # sent, but the result was never recorded
    toolkit.store.update_job(job_id, status="running")
    toolkit.engine.recover()
    run_to_end(toolkit, job_id)
    done = toolkit.store.get_item(job_id, item["id"])
    assert done["status"] == "succeeded" and done["result"]["reconciled"]
    assert connector.find_governance_groups("Ops Team")[0]["member_count"] == 2


def test_rollback_deletes_created_groups(toolkit, sandbox):
    job_id = make_job_gg(toolkit, sandbox, b"name,owner,members\nTemp Team,alice,bob\n")
    approve(toolkit, job_id)
    run_to_end(toolkit, job_id)
    rb = toolkit.rollback(job_id)
    wait_for(lambda: toolkit.store.get_job(rb)["status"] == "review")
    approve(toolkit, rb)
    run_to_end(toolkit, rb)
    assert toolkit.connections.build(sandbox["id"]).find_governance_groups("Temp Team") == []


def test_no_missing_groups_rejected(toolkit, sandbox):
    job_id = make_job(toolkit, sandbox)
    with pytest.raises(JobStateError):
        toolkit.create_missing_governance_groups(job_id, owner="alice")


class Recorder:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        from tests.test_units import FakeResp
        return FakeResp(200, self.body)


class StaticToken:
    client_id = "x"
    token_url = "https://acme.api.identitynow.com/oauth/token"

    def get(self, force=False):
        return "t"

    def invalidate(self):
        pass


def test_sailpoint_group_lookup_uses_workgroups_endpoint():
    session = Recorder([{"id": "a" * 32, "name": "Finance Approvers", "memberCount": 3,
                         "owner": {"displayName": "Alice"}}])
    isc = SailPointISC({"base_url": "https://acme.api.identitynow.com", "workgroups_api": "v2025"}, None,
                       token=StaticToken(), session=session)
    found = isc.find_governance_groups("Finance Approvers")
    assert found == [{"id": "a" * 32, "name": "Finance Approvers", "owner": "Alice", "member_count": 3}]
    method, url, kw = session.calls[0]
    assert url.endswith("/v2025/workgroups") and kw["params"]["filters"] == 'name eq "Finance Approvers"'
    assert kw["headers"]["X-SailPoint-Experimental"] == "true"


def test_csv_creates_missing_groups_when_rows_run(toolkit, sandbox):
    csv = (b"name,owner,source,entitlements,approval_schemes,new_group_owner,new_group_members\n"
           b"AP1,alice,Active Directory,Finance-Read,manager;governance_group:Audit Team,carol,alice;bob\n"
           b"AP2,alice,Active Directory,HR-Read,governance_group:audit team,carol,bob;alice\n")
    job_id = make_job(toolkit, sandbox, csv)
    planned = rows(job_id, toolkit)
    assert [i["op"] for i in planned] == ["create", "create"]
    assert planned[0]["payload"]["accessRequestConfig"]["approvalSchemes"][1] == {
        "approverType": "GOVERNANCE_GROUP", "approverId": None, "newGroup": "Audit Team"}
    assert planned[0]["payload"]["new_governance_groups"][0]["group"]["owner"]["name"]
    view = toolkit.job_view(job_id)
    assert view["missing_governance_groups"] == [] and view["new_governance_groups"] == ["Audit Team"]
    summary = toolkit_confirm_lines(toolkit, job_id, "approve")
    assert any("will be created from the CSV: Audit Team" in line for line in summary)

    approve(toolkit, job_id)
    run_to_end(toolkit, job_id)
    done = rows(job_id, toolkit)
    assert [i["status"] for i in done] == ["succeeded", "succeeded"]
    connector = toolkit.connections.build(sandbox["id"])
    group = connector.find_governance_groups("Audit Team")
    assert len(group) == 1 and group[0]["member_count"] == 2
    assert done[0]["result"]["created_groups"] == [{"id": group[0]["id"], "name": "Audit Team"}]
    assert done[1]["result"]["created_groups"] == []  # reused the group created by the first row
    profile = connector.get_access_profile(done[0]["remote_id"])
    assert profile["accessRequestConfig"]["approvalSchemes"][1] == {"approverType": "GOVERNANCE_GROUP",
                                                                    "approverId": group[0]["id"]}
    assert toolkit.store.created_governance_groups(job_id) == done[0]["result"]["created_groups"]


def toolkit_confirm_lines(toolkit, job_id, command):
    from amtoolkit.confirm import Confirmer
    return Confirmer(toolkit).job_summary(job_id, command)["lines"]


def test_csv_group_definitions_must_agree(toolkit, sandbox):
    csv = (b"name,owner,source,entitlements,approval_schemes,new_group_owner,new_group_members\n"
           b"AP1,alice,Active Directory,Finance-Read,governance_group:Audit Team,carol,alice\n"
           b"AP2,alice,Active Directory,HR-Read,governance_group:Audit Team,bob,alice\n")
    planned = rows(make_job(toolkit, sandbox, csv), toolkit)
    assert [i["op"] for i in planned] == ["create", "error"]
    assert any(m["code"] == "gg_conflict" for m in planned[1]["messages"])


def test_csv_group_with_unknown_owner_is_an_error(toolkit, sandbox):
    csv = (b"name,owner,source,entitlements,approval_schemes,new_group_owner\n"
           b"AP1,alice,Active Directory,Finance-Read,governance_group:Audit Team,nobody@example.com\n")
    item = rows(make_job(toolkit, sandbox, csv), toolkit)[0]
    assert item["op"] == "error"
    assert any(m["field"] == "new_group_owner" and m["level"] == "error" for m in item["messages"])
