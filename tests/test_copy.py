"""Copying objects between environments: a "test" sandbox to a "production" sandbox whose IDs all differ."""
import json

import pytest

from amtoolkit.connectors.mock import SEED
from tests.conftest import wait_for
from tests.test_experimental import items
from tests.test_workflow import run_to_end

ALICE, BOB, CAROL = (f"2c9180835d2e5168015d32f890ca158{n}" for n in (1, 2, 3))


def prod_id(test_id):
    return "9f918" + test_id[5:]


@pytest.fixture
def envs(toolkit):
    test = toolkit.save_connection({"name": "Test", "kind": "mock", "environment": "test",
                                    "config": {"latency_ms": "0", "failure_rate": "0"}})
    prod = toolkit.save_connection({"name": "Prod", "kind": "mock", "environment": "production",
                                    "config": {"latency_ms": "0", "failure_rate": "0"}})
    # Production has its own IDs for everything, and its own cluster name.
    state = json.loads(json.dumps(SEED).replace('"2c918', '"9f918').replace("Main VA cluster", "Prod VA cluster"))
    (toolkit.data_dir / f"sandbox_{prod['id']}.json").write_text(json.dumps(state), "utf-8")
    return test, prod


def run(toolkit, job_id):
    toolkit.approve(job_id, toolkit.store.plan_fingerprint(job_id), "Prod")  # production asks for its name
    run_to_end(toolkit, job_id)
    return toolkit.store.job_counts(job_id)["status"]


def rollback(toolkit, job_id):
    rb = toolkit.rollback(job_id)
    wait_for(lambda: toolkit.store.get_job(rb)["status"] == "review")
    return rb


def conn(toolkit, c):
    return toolkit.connections.build(c["id"])


def copy_job(toolkit, envs, csv, **options):
    test, prod = envs
    job_id = toolkit.create_job(name="copy", action_key="object.copy", connection_id=prod["id"],
                                options={"source_connection_id": test["id"], **options}, filename="x.csv",
                                data=csv.encode())
    wait_for(lambda: toolkit.store.get_job(job_id)["status"] == "review")
    return job_id


def ent_id(c, source, name):
    src = c.find_sources(source)[0]["id"]
    return c.find_entitlements(src, name)[0]["id"]


def make_profile(c, name, owner, ents, description="", schemes=(), requestable=True):
    src = c.find_sources("Active Directory")[0]
    c.create_access_profile({
        "name": name, "description": description,
        "owner": {"type": "IDENTITY", "id": owner, "name": c.find_identities(owner)[0]["name"]},
        "source": {"type": "SOURCE", "id": src["id"], "name": src["name"]},
        "entitlements": [{"type": "ENTITLEMENT", "id": ent_id(c, "Active Directory", e), "name": e} for e in ents],
        "enabled": True, "requestable": requestable,
        "accessRequestConfig": {"commentsRequired": False, "denialCommentsRequired": False,
                                "approvalSchemes": list(schemes)}})


def test_job_needs_a_different_source_connection(toolkit, envs):
    test, prod = envs
    with pytest.raises(ValueError, match="copy from"):
        toolkit.create_job(name="x", action_key="object.copy", connection_id=prod["id"], options={},
                           filename="x.csv", data=b"object_type,object\nrole,X\n")
    with pytest.raises(ValueError, match="different connection"):
        toolkit.create_job(name="x", action_key="object.copy", connection_id=prod["id"],
                           options={"source_connection_id": prod["id"]}, filename="x.csv",
                           data=b"object_type,object\nrole,X\n")


def test_creates_access_profile_with_references_translated_to_target_ids(toolkit, envs):
    test, prod = envs
    t = conn(toolkit, test)
    group = t.find_governance_groups("Finance Approvers")[0]["id"]
    make_profile(t, "Finance - Read", CAROL, ["Finance-Read", "VPN Users"], "Read finance",
                 [{"approverType": "MANAGER", "approverId": None},
                  {"approverType": "GOVERNANCE_GROUP", "approverId": group}])
    job_id = copy_job(toolkit, envs, "object_type,object\naccess_profile,Finance - Read\n")
    [item] = items(toolkit, job_id)
    assert item["op"] == "create", item["messages"]
    body = item["payload"]["create"]
    assert body["owner"]["id"] == prod_id(CAROL)
    p = conn(toolkit, prod)
    assert sorted(e["id"] for e in body["entitlements"]) == sorted(
        ent_id(p, "Active Directory", e) for e in ("Finance-Read", "VPN Users"))
    assert body["accessRequestConfig"]["approvalSchemes"][1]["approverId"] == prod_id(group)
    assert run(toolkit, job_id)["succeeded"] == 1
    created = p.find_objects("access_profile", "Finance - Read")[0]
    assert created["owner"] == prod_id(CAROL) and created["description"] == "Read finance"
    assert created["approval_schemes"] == ["manager", f"governance_group:{prod_id(group)}"]

    rb = rollback(toolkit, job_id)
    assert [i["op"] for i in items(toolkit, rb)] == ["delete"]
    run(toolkit, rb)
    assert p.find_objects("access_profile", "Finance - Read") == []


def test_updates_only_the_chosen_attributes_and_rolls_back(toolkit, envs):
    test, prod = envs
    make_profile(conn(toolkit, test), "Payroll", BOB, ["HR-Read"], "Test text", requestable=False)
    p = conn(toolkit, prod)
    make_profile(p, "Payroll", prod_id(ALICE), ["HR-Read"], "Prod text")
    job_id = copy_job(toolkit, envs, "object_type,object,attributes\naccess_profile,Payroll,description;owner\n")
    [item] = items(toolkit, job_id)
    assert item["op"] == "update", item["messages"]
    assert item["payload"]["after"] == {"description": "Test text", "owner": prod_id(BOB)}
    assert item["payload"]["labels"]["owner"] == ["'Alice Admin'", "'Bob Builder'"]
    run(toolkit, job_id)
    after = p.find_objects("access_profile", "Payroll")[0]
    assert (after["description"], after["owner"], after["requestable"]) == ("Test text", prod_id(BOB), True)

    rb = rollback(toolkit, job_id)
    assert run(toolkit, rb)["succeeded"] == 1
    back = p.find_objects("access_profile", "Payroll")[0]
    assert (back["description"], back["owner"]) == ("Prod text", prod_id(ALICE))


def test_identical_objects_are_skipped_and_keep_is_respected(toolkit, envs):
    test, prod = envs
    make_profile(conn(toolkit, test), "Same", ALICE, ["HR-Read"], "Same text", requestable=False)
    make_profile(conn(toolkit, prod), "Same", prod_id(ALICE), ["HR-Read"], "Same text")
    job_id = copy_job(toolkit, envs, "object_type,object,keep\naccess_profile,Same,requestable\n")
    assert [i["op"] for i in items(toolkit, job_id)] == ["skip"]


def test_source_keeps_environment_specific_settings(toolkit, envs):
    test, prod = envs
    t = conn(toolkit, test)
    ad = t.find_objects("source", "Active Directory")[0]["id"]
    t.patch_object("source", ad, {"description": "Domain accounts", "config.pageSize": "1000",
                                  "config.host": "dc01.test.local"})
    job_id = copy_job(toolkit, envs, "object_type,object\nsource,Active Directory\n")
    [item] = items(toolkit, job_id)
    assert item["op"] == "update", item["messages"]
    assert item["payload"]["after"] == {"description": "Domain accounts", "config.pageSize": "1000"}
    kept = next(m["text"] for m in item["messages"] if m["code"] == "kept")
    assert "host" in kept and "cluster" in kept
    run(toolkit, job_id)
    prod_ad = conn(toolkit, prod).find_objects("source", "Active Directory")[0]
    assert prod_ad["config"]["host"] == "dc01.example.local" and prod_ad["config"]["pageSize"] == "1000"

    job2 = copy_job(toolkit, envs, "object_type,object,attributes,overrides\n"
                                   "source,Active Directory,description,config.host=dc01.prod.local\n")
    assert items(toolkit, job2)[0]["payload"]["after"] == {"config.host": "dc01.prod.local"}

    job3 = copy_job(toolkit, envs, "object_type,object,attributes,overrides\n"
                                   "source,Active Directory,description,\"config.searchDNs=OU=A,DC=x|OU=B,DC=x;config.useSSL=no\"\n")
    assert items(toolkit, job3)[0]["payload"]["after"] == {"config.searchDNs": ["OU=A,DC=x", "OU=B,DC=x"],
                                                           "config.useSSL": False}


def test_secrets_are_never_copied(toolkit, envs):
    job_id = copy_job(toolkit, envs, "object_type,object,attributes\nsource,Active Directory,config.password\n")
    [item] = items(toolkit, job_id)
    assert item["op"] == "error"
    assert any(m["code"] == "secret" for m in item["messages"])


def test_new_source_needs_the_target_cluster(toolkit, envs):
    test, prod = envs
    t = conn(toolkit, test)
    t.create_source({"name": "LDAP", "owner": {"type": "IDENTITY", "id": ALICE}, "connector": "ldap",
                     "cluster": {"type": "CLUSTER", "id": "2c9180887a1b2c3d017a1b2c3d4e0001", "name": "Main VA cluster"},
                     "connectorAttributes": {"host": "ldap.test.local", "pageSize": "100", "password": "x"}})
    job_id = copy_job(toolkit, envs, "object_type,object\nsource,LDAP\n")
    assert any(m["code"] == "cluster_required" for m in items(toolkit, job_id)[0]["messages"])

    job2 = copy_job(toolkit, envs, "object_type,object,overrides\n"
                                   "source,LDAP,cluster=Prod VA cluster;config.host=ldap.prod.local\n")
    [item] = items(toolkit, job2)
    assert item["op"] == "create", item["messages"]
    body = item["payload"]["create"]
    assert body["cluster"]["id"] == prod_id("2c9180887a1b2c3d017a1b2c3d4e0001")
    assert body["connectorAttributes"] == {"host": "ldap.prod.local", "pageSize": "100"}
    assert any(m["code"] == "secret" and m["level"] == "warning" for m in item["messages"])
    assert run(toolkit, job2)["succeeded"] == 1


def test_references_to_objects_created_earlier_in_the_job(toolkit, envs):
    test, prod = envs
    t = conn(toolkit, test)
    group = t.create_governance_group({"name": "Payroll Approvers", "description": "Payroll",
                                       "owner": {"type": "IDENTITY", "id": ALICE}})
    t.add_governance_group_members(group["id"], [CAROL])
    make_profile(t, "Payroll Admin", CAROL, ["HR-Write"],
                 schemes=[{"approverType": "GOVERNANCE_GROUP", "approverId": group["id"]}])
    job_id = copy_job(toolkit, envs, "object_type,object,attributes\n"
                                     "governance_group,Payroll Approvers,\n"
                                     "access_profile,Payroll Admin,\n")
    group_row, profile_row = items(toolkit, job_id)
    assert [group_row["op"], profile_row["op"]] == ["create", "create"], profile_row["messages"]
    assert any(m["code"] == "copy_pending" for m in profile_row["messages"])
    # Re-planning the second row on its own still knows that the first one creates the group.
    assert toolkit.replan_item(job_id, profile_row["id"])["op"] == "create"
    assert run(toolkit, job_id)["succeeded"] == 2
    p = conn(toolkit, prod)
    new_group = p.find_governance_groups("Payroll Approvers")[0]
    assert new_group["member_count"] == 0  # members are environment-specific
    assert p.find_objects("access_profile", "Payroll Admin")[0]["approval_schemes"] == \
        [f"governance_group:{new_group['id']}"]


def test_compare_and_create_job_from_the_selection(toolkit, envs):
    test, prod = envs
    make_profile(conn(toolkit, test), "Payroll", BOB, ["HR-Read"], "Test text")
    make_profile(conn(toolkit, test), "Only in test", ALICE, ["HR-Read"])
    make_profile(conn(toolkit, prod), "Payroll", prod_id(ALICE), ["HR-Read"], "Prod text")
    compare_id = toolkit.compare.start(test["id"], prod["id"], "access_profile")
    result = wait_for(lambda: (r := toolkit.compare.get(compare_id))["status"] == "completed" and r)
    rows = {r["name"]: r for r in result["rows"]}
    assert rows["Only in test"]["status"] == "new"
    assert rows["Legacy VPN"]["status"] == "same"
    diffs = {d["attr"]: (d["source"], d["target"]) for d in rows["Payroll"]["diffs"]}
    assert diffs == {"description": ("Test text", "Prod text"), "owner": ("Bob Builder", "Alice Admin")}

    job_id = toolkit.create_copy_job(compare_id, [
        {"source_id": rows["Payroll"]["source_id"], "attributes": ["description"]},
        {"source_id": rows["Only in test"]["source_id"]},
        {"source_id": rows["Legacy VPN"]["source_id"]},  # identical: ignored
    ])
    wait_for(lambda: toolkit.store.get_job(job_id)["status"] == "review")
    planned = items(toolkit, job_id)
    assert [(i["input"]["attributes"], i["op"]) for i in planned] == [("description", "update"), ("", "create")]
    assert planned[0]["payload"]["after"] == {"description": "Test text"}
    assert toolkit.job_view(job_id)["source_connection"]["name"] == "Test"


def test_compare_flags_environment_specific_source_settings(toolkit, envs):
    test, prod = envs
    t = conn(toolkit, test)
    ad = t.find_objects("source", "Active Directory")[0]["id"]
    t.patch_object("source", ad, {"config.host": "dc01.test.local", "config.pageSize": "1000"})
    compare_id = toolkit.compare.start(test["id"], prod["id"], "source")
    result = wait_for(lambda: (r := toolkit.compare.get(compare_id))["status"] == "completed" and r)
    row = next(r for r in result["rows"] if r["name"] == "Active Directory")
    flags = {d["attr"]: d["env_specific"] for d in row["diffs"]}
    assert flags["config.host"] is True and flags["config.pageSize"] is False
    assert flags["cluster"] is True  # the cluster names differ between the environments
    assert "password" in row["secrets"]


def test_role_access_profiles_are_matched_by_name(toolkit, envs):
    test, prod = envs
    t, p = conn(toolkit, test), conn(toolkit, prod)
    for c, owner in ((t, ALICE), (p, prod_id(ALICE))):
        make_profile(c, "Finance - Read", owner, ["Finance-Read"])
    ap = t.find_objects("access_profile", "Finance - Read")[0]
    t.create_role({"name": "Analyst", "description": "Analysts", "owner": {"type": "IDENTITY", "id": BOB},
                   "accessProfiles": [{"type": "ACCESS_PROFILE", "id": ap["id"], "name": ap["name"]}],
                   "requestable": True, "enabled": True})
    job_id = copy_job(toolkit, envs, "object_type,object\nrole,Analyst\n")
    [item] = items(toolkit, job_id)
    assert item["op"] == "create", item["messages"]
    prod_ap = p.find_objects("access_profile", "Finance - Read")[0]["id"]
    assert [x["id"] for x in item["payload"]["create"]["accessProfiles"]] == [prod_ap]
    assert run(toolkit, job_id)["succeeded"] == 1
    assert p.find_objects("role", "Analyst")[0]["access_profiles"] == [prod_ap]


def test_group_members_are_copied_only_when_named(toolkit, envs):
    test, prod = envs
    t, p = conn(toolkit, test), conn(toolkit, prod)
    group = t.find_governance_groups("Finance Approvers")[0]["id"]
    t.add_governance_group_members(group, [BOB])
    job_id = copy_job(toolkit, envs, "object_type,object\ngovernance_group,Finance Approvers\n")
    assert items(toolkit, job_id)[0]["op"] == "skip"

    job2 = copy_job(toolkit, envs, "object_type,object,attributes\ngovernance_group,Finance Approvers,members\n")
    [item] = items(toolkit, job2)
    assert item["op"] == "update", item["messages"]
    assert any(m["code"] == "env_specific" for m in item["messages"])
    assert "Bob Builder" in item["payload"]["labels"]["members"][1]
    run(toolkit, job2)
    prod_group = prod_id(group)
    assert prod_id(BOB) in {m["id"] for m in p.list_governance_group_members(prod_group)}
    run(toolkit, rollback(toolkit, job2))
    assert prod_id(BOB) not in {m["id"] for m in p.list_governance_group_members(prod_group)}


# ---------------------------------------------------------------------------- Copy page: one object, no CSV
def test_object_picker_lists_names(toolkit, envs):
    test, _ = envs
    make_profile(conn(toolkit, test), "Payroll", BOB, ["HR-Read"])
    assert toolkit.list_object_names(test["id"], "access_profile") == ["Legacy VPN", "Payroll"]
    assert toolkit.list_object_names(test["id"], "access_profile", "pay") == ["Payroll"]
    assert "Active Directory" in toolkit.list_object_names(test["id"], "source")


def test_preview_lists_every_attribute_side_by_side(toolkit, envs):
    test, prod = envs
    make_profile(conn(toolkit, test), "Payroll", BOB, ["HR-Read"], "Test text")
    make_profile(conn(toolkit, prod), "Payroll", prod_id(ALICE), ["HR-Read"], "Prod text")
    row = toolkit.copy_preview({"source_connection_id": test["id"], "connection_id": prod["id"],
                                "object_type": "access_profile", "object": "Payroll"})
    attrs = {d["attr"]: d for d in row["diffs"]}
    assert row["status"] == "different"
    assert (attrs["description"]["source"], attrs["description"]["target"], attrs["description"]["same"]) == \
        ("Test text", "Prod text", False)
    assert attrs["entitlements"]["same"] is True and attrs["owner"]["required"] is True


def move(client, toolkit, req, typed="Prod"):
    planned = client.post("/api/copy/plan", json=req).get_json()
    confirmation = {"token": planned["confirmation"]["token"], "acknowledged": True, "text": typed}
    return planned, client.post("/api/copy/move", json=dict(req, confirmation=confirmation))


def test_move_selected_values_from_the_copy_page(client, toolkit, envs):
    test, prod = envs
    make_profile(conn(toolkit, test), "Payroll", BOB, ["HR-Read"], "Test text", requestable=False)
    p = conn(toolkit, prod)
    make_profile(p, "Payroll", prod_id(ALICE), ["HR-Read"], "Prod text")
    req = {"source_connection_id": test["id"], "connection_id": prod["id"], "object_type": "access_profile",
           "object": "Payroll", "attributes": ["description"]}
    planned, resp = move(client, toolkit, req)
    assert planned["op"] == "update"
    assert "Description: 'Prod text' → 'Test text'" in planned["confirmation"]["lines"]
    assert planned["confirmation"]["requires_text"] == "Prod"
    assert resp.status_code == 201, resp.get_json()
    job_id = resp.get_json()["id"]
    wait_for(lambda: toolkit.store.get_job(job_id)["status"] == "completed")
    after = p.find_objects("access_profile", "Payroll")[0]
    assert (after["description"], after["owner"], after["requestable"]) == ("Test text", prod_id(ALICE), True)
    assert toolkit.job_view(job_id)["allowed"]["rollback"]


def test_move_is_refused_when_the_target_changed_after_review(client, toolkit, envs):
    test, prod = envs
    make_profile(conn(toolkit, test), "Payroll", BOB, ["HR-Read"], "Test text")
    p = conn(toolkit, prod)
    make_profile(p, "Payroll", prod_id(ALICE), ["HR-Read"], "Prod text")
    req = {"source_connection_id": test["id"], "connection_id": prod["id"], "object_type": "access_profile",
           "object": "Payroll", "attributes": ["description"]}
    planned = client.post("/api/copy/plan", json=req).get_json()
    p.patch_object("access_profile", p.find_objects("access_profile", "Payroll")[0]["id"],
                   {"description": "Edited in prod meanwhile"})
    confirmation = {"token": planned["confirmation"]["token"], "acknowledged": True, "text": "Prod"}
    resp = client.post("/api/copy/move", json=dict(req, confirmation=confirmation))
    assert resp.status_code == 428
    assert toolkit.store.list_jobs() == []  # the discarded attempt leaves nothing behind
    assert p.find_objects("access_profile", "Payroll")[0]["description"] == "Edited in prod meanwhile"


def test_new_object_gets_only_the_chosen_attributes_plus_required_ones(client, toolkit, envs):
    test, prod = envs
    make_profile(conn(toolkit, test), "Payroll", BOB, ["HR-Read"], "Test text", requestable=False)
    req = {"source_connection_id": test["id"], "connection_id": prod["id"], "object_type": "access_profile",
           "object": "Payroll", "attributes": ["description"]}
    planned, resp = move(client, toolkit, req)
    assert planned["op"] == "create", planned["messages"]
    wait_for(lambda: toolkit.store.get_job(resp.get_json()["id"])["status"] == "completed")
    created = conn(toolkit, prod).find_objects("access_profile", "Payroll")[0]
    assert (created["description"], created["owner"], created["requestable"]) == ("Test text", prod_id(BOB), True)
    assert toolkit.store.get_job(resp.get_json()["id"])["name"] == "Copy access profile Payroll from Test to Prod"


# ---------------------------------------------------------------------------- demo environments
def test_demo_environments_show_each_kind_of_difference(client, toolkit):
    test, prod = toolkit.create_demo_environments()
    assert (test["environment"], prod["environment"]) == ("test", "production")

    def preview(type_, name):
        return toolkit.copy_preview({"source_connection_id": test["id"], "connection_id": prod["id"],
                                     "object_type": type_, "object": name})

    def plan(type_, name, attributes=()):
        req = toolkit.copy_request({"source_connection_id": test["id"], "connection_id": prod["id"],
                                    "object_type": type_, "object": name, "attributes": list(attributes)})
        return toolkit.plan_copy(req)

    finance = {d["attr"]: d for d in preview("access_profile", "Finance - Read")["diffs"]}
    assert not finance["description"]["same"] and finance["requestable"]["same"]
    assert finance["entitlements"]["source"] == ["Finance-Read", "VPN Users"]
    assert preview("access_profile", "HR - Read")["status"] == "same"
    assert preview("access_profile", "Helpdesk Tools")["status"] == "new"
    ad = {d["attr"]: d for d in preview("source", "Active Directory")["diffs"]}
    assert ad["config.host"]["env_specific"] and not ad["config.pageSize"]["env_specific"]

    assert plan("access_profile", "Finance - Read").op == "update"
    assert plan("access_profile", "Helpdesk Tools").op == "create"
    for name, problem in (("Sales - Write", "Tess Tester"), ("Cloud Admin", "Cloud-Admins"),
                          ("IT Admin Access", "IT Approvers")):
        result = plan("access_profile", name, ["owner"] if name == "Sales - Write" else ())
        assert result.op == "error" and any(problem in m["text"] for m in result.messages), (name, result.messages)

    # Running it again resets the demo data.
    p = toolkit.connections.build(prod["id"])
    ap = p.find_objects("access_profile", "Finance - Read")[0]
    p.patch_object("access_profile", ap["id"], {"description": "changed"})
    toolkit.create_demo_environments()
    assert p.find_objects("access_profile", "Finance - Read")[0]["description"] == "Finance read"
    assert len(toolkit.store.list_connections()) == 2


# ---------------------------------------------------------------------------- scans, change times, history
def demo_compare(toolkit, type_):
    test, prod = toolkit.create_demo_environments()
    compare_id = toolkit.compare.start(test["id"], prod["id"], type_)
    result = wait_for(lambda: (r := toolkit.compare.get(compare_id))["status"] == "completed" and r, timeout=30)
    return test, prod, compare_id, {r["name"]: r for r in result["rows"]}, result


def test_source_scan_compares_entitlement_catalogs(toolkit):
    _, _, compare_id, rows, result = demo_compare(toolkit, "source")
    assert rows["Active Directory"]["entitlements"]["only_in_source"] == ["Cloud-Admins"]
    assert rows["Workday"]["entitlements"]["only_in_target"] == ["Contractor"]
    assert rows["Salesforce"]["entitlements_differ"] and rows["ServiceNow"]["status"] == "new"
    assert rows["ServiceNow"]["entitlements"]["target_count"] is None
    assert result["summary"]["entitlements_differ"] == 3 and result["summary"]["new"] == 1
    csv = toolkit.compare_csv(compare_id)
    assert "Cloud-Admins" in csv and "only in Demo Prod" in csv and "pageSize" in csv


def test_comparison_marks_which_side_changed_more_recently(toolkit):
    test, prod, _, rows, _ = demo_compare(toolkit, "access_profile")
    assert rows["Finance - Read"]["newer"] == "source"
    assert rows["Finance - Write"]["newer"] == "target"  # changed in production after test
    req = toolkit.copy_request({"source_connection_id": test["id"], "connection_id": prod["id"],
                                "object_type": "access_profile", "object": "Finance - Write"})
    plan = toolkit.plan_copy(req)
    assert plan.op == "update" and any(m["code"] == "target_newer" for m in plan.messages)


def test_attribute_history_from_audit_events_and_toolkit_jobs(client, toolkit):
    test, prod = toolkit.create_demo_environments()
    p = toolkit.connections.build(prod["id"])
    payroll = p.find_objects("access_profile", "Payroll Admin")[0]["id"]
    history = toolkit.object_history(prod["id"], "access_profile", payroll, "enabled")
    # Events that do not record values (here: the creation) are kept, since they may still be the change.
    [event] = [e for e in history["tenant"] if e["values_known"]]
    assert any(not e["values_known"] for e in history["tenant"])
    assert (event["actor"], event["changes"]["enabled"]) == ("Frank Finance", {"old": True, "new": False})

    req = {"source_connection_id": test["id"], "connection_id": prod["id"], "object_type": "access_profile",
           "object": "Payroll Admin", "attributes": ["enabled"]}
    planned = client.post("/api/copy/plan", json=req).get_json()
    resp = client.post("/api/copy/move", json=dict(req, confirmation={
        "token": planned["confirmation"]["token"], "acknowledged": True, "text": "Demo Prod"}))
    wait_for(lambda: toolkit.store.get_job(resp.get_json()["id"])["status"] == "completed", timeout=20)
    history = client.get(f"/api/connections/{prod['id']}/history?type=access_profile&id={payroll}"
                         "&attribute=enabled").get_json()
    [mine] = history["toolkit"]
    assert mine["changes"]["enabled"] == {"old": "no", "new": "yes"} and mine["approved_by"]
    assert history["tenant"][0]["changes"]["enabled"] == {"old": False, "new": True}  # the sandbox's own audit trail
    assert len([e for e in history["tenant"] if e["values_known"]]) == 2
    everything = toolkit.object_history(prod["id"], "access_profile", payroll)
    assert any(e["action"] == "Create access profile" for e in everything["tenant"])


def test_marking_an_attribute_environment_specific(toolkit, envs, client):
    test, prod = envs
    t = conn(toolkit, test)
    ad = t.find_objects("source", "Active Directory")[0]["id"]
    t.patch_object("source", ad, {"description": "Test AD", "config.pageSize": "1000"})
    compare_id = toolkit.compare.start(test["id"], prod["id"], "source")
    wait_for(lambda: toolkit.compare.get(compare_id)["status"] == "completed")

    r = client.put("/api/env-marks", json={"object_type": "source", "attribute": "config.pageSize",
                                           "env_specific": True})
    assert r.status_code == 200 and r.get_json() == {"object_type": "source", "attribute": "config.pageSize",
                                                     "env_specific": True, "marked": True}
    client.put("/api/env-marks", json={"object_type": "source", "attribute": "cluster", "env_specific": False})
    assert client.get("/api/env-marks").get_json() == [
        {"object_type": "source", "attribute": "cluster", "env_specific": False},
        {"object_type": "source", "attribute": "config.pageSize", "env_specific": True}]

    # An earlier comparison picks the marks up, and so do new ones
    for cid in (compare_id, toolkit.compare.start(test["id"], prod["id"], "source")):
        result = wait_for(lambda: (r := toolkit.compare.get(cid))["status"] == "completed" and r)
        row = next(r for r in result["rows"] if r["name"] == "Active Directory")
        flags = {d["attr"]: (d["env_specific"], d["env_marked"]) for d in row["diffs"]}
        assert flags["config.pageSize"] == (True, True) and flags["cluster"] == (False, True)
        assert flags["description"] == (False, False)

    # Plans keep a marked attribute unless it is named, and copy an unmarked one: here the cluster, whose
    # name differs in production
    req = toolkit.copy_request({"source_connection_id": test["id"], "connection_id": prod["id"],
                                "object_type": "source", "object": "Active Directory"})
    plan = toolkit.plan_copy(req)
    assert plan.op == "error" and "Main VA cluster" in plan.messages[0]["text"]
    toolkit.mark_env_specific("source", "cluster", True)
    plan = toolkit.plan_copy(req)
    assert plan.op == "update" and set(plan.payload["after"]) == {"description"}

    # Marking it the built-in way removes the mark; secrets and unknown attributes cannot be marked
    toolkit.mark_env_specific("source", "config.pageSize", False)
    assert toolkit.env_marks() == []
    for attr in ("config.password", "nonsense"):
        assert client.put("/api/env-marks", json={"object_type": "source", "attribute": attr,
                                                  "env_specific": True}).status_code == 400
