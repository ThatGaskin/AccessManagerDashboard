"""Demo data for a pair of sandboxes — "test" and "production" — that differ on purpose, so the Copy and
Compare pages have something to show: changed values, objects that exist on one side only, different
group members, environment-specific source settings, and references that cannot be matched.

Each sandbox has its own IDs (as real tenants do); objects and people are matched by name and email.
"""
import hashlib
import time

TEST, PROD = "demo-test", "demo-production"
FLAVORS = (TEST, PROD)

PEOPLE = {  # alias: (name, in test, in production)
    "alice": ("Alice Admin", True, True), "bob": ("Bob Builder", True, True), "carol": ("Carol Owner", True, True),
    "dave": ("Dave Analyst", True, True), "erin": ("Erin Engineer", True, True), "frank": ("Frank Finance", True, True),
    "gina": ("Gina Helpdesk", True, True),
    "tess": ("Tess Tester", True, False),  # a test account: owning something with it cannot be copied
}

ENTITLEMENTS = {
    "Active Directory": ["Domain Users", "VPN Users", "Finance-Read", "Finance-Write", "HR-Read", "HR-Write", "Sales-Read",
                         "Sales-Write", "IT-Admins", "Helpdesk"],
    "Workday": ["Employee", "Manager", "HR Partner", "Payroll Admin"],
    "Salesforce": ["Standard User", "Sales Manager", "System Administrator", "Read Only"],
    "ServiceNow": ["itil", "admin"],
}
TEST_ONLY_ENTITLEMENTS = {"Active Directory": ["Cloud-Admins"],  # not aggregated in production yet
                          "Salesforce": ["Marketing User"]}
PROD_ONLY_ENTITLEMENTS = {"Workday": ["Contractor"]}

SOURCES = {
    TEST: {
        "Active Directory": dict(description="Corporate AD – all employees and contractors", owner="alice",
                                 deleteThreshold=20, connector="active-directory", cluster=True, connectorAttributes={
                                     "host": "dc01.test.acme.local", "port": "636", "useSSL": True,
                                     "user": "ACMETEST\\svc-isc", "password": "(secret)",
                                     "searchDNs": ["OU=Users,DC=test,DC=acme,DC=local"], "pageSize": "1000",
                                     "manageRecycleBin": True}),
        "Workday": dict(description="HR system (implementation tenant)", owner="carol", authoritative=True,
                        deleteThreshold=10, connector="workday", connectorAttributes={
                            "url": "https://wd5-impl-services1.workday.com/ccx/service/acme_preview",
                            "user": "ISU_ISC_TEST", "password": "(secret)", "pageSize": "200",
                            "includeFutureHires": True}),
        "Salesforce": dict(description="CRM", owner="bob", deleteThreshold=10, connector="salesforce",
                           connectorAttributes={"url": "https://acme--uat.sandbox.my.salesforce.com",
                                                "clientId": "3MVG9test", "clientSecret": "(secret)",
                                                "apiVersion": "60.0"}),
        "ServiceNow": dict(description="IT service management", owner="erin", deleteThreshold=10,
                           connector="servicenow", connectorAttributes={
                               "url": "https://acme-dev.service-now.com", "user": "svc_isc",
                               "password": "(secret)", "pageSize": "100"}),
    },
    PROD: {
        "Active Directory": dict(description="Corporate Active Directory", owner="alice", deleteThreshold=10,
                                 connector="active-directory", cluster=True, connectorAttributes={
                                     "host": "dc01.acme.local", "port": "636", "useSSL": True,
                                     "user": "ACME\\svc-isc", "password": "(secret)",
                                     "searchDNs": ["OU=Users,DC=acme,DC=local"], "pageSize": "500",
                                     "manageRecycleBin": False}),
        "Workday": dict(description="HR system", owner="frank", authoritative=True, deleteThreshold=10,
                        connector="workday", connectorAttributes={
                            "url": "https://wd5-services1.workday.com/ccx/service/acme", "user": "ISU_ISC",
                            "password": "(secret)", "pageSize": "200", "includeFutureHires": False}),
        "Salesforce": dict(description="CRM", owner="bob", deleteThreshold=10, connector="salesforce",
                           connectorAttributes={"url": "https://acme.my.salesforce.com", "clientId": "3MVG9prod",
                                                "clientSecret": "(secret)", "apiVersion": "59.0"}),
    },
}

GROUPS = {  # name: (description, owner, members)
    TEST: {
        "Finance Approvers": ("Approves finance access", "alice", ["alice", "carol", "frank"]),
        "HR Approvers": ("Approves HR access", "carol", ["carol", "frank"]),
        "IT Approvers": ("Approves IT and admin access", "erin", ["erin", "gina"]),
    },
    PROD: {
        "Finance Approvers": ("Approves access to finance systems", "alice", ["alice", "carol"]),
        "HR Approvers": ("Approves HR access", "carol", ["carol", "frank"]),
        "Audit Team": ("Reviews privileged access", "alice", ["alice"]),
    },
}

# name: dict(description, owner, source, entitlements, approvers, requestable, enabled, comments)
PROFILES = {
    TEST: {
        "Finance - Read": dict(description="Read access to finance shares", owner="carol",
                               entitlements=["Finance-Read", "VPN Users"],
                               approvers=["manager", "group:Finance Approvers"]),
        "Finance - Write": dict(description="Change finance data", owner="carol", entitlements=["Finance-Write"],
                                approvers=["manager", "group:Finance Approvers"], comments=True),
        "HR - Read": dict(description="Read HR folders", owner="carol", entitlements=["HR-Read"],
                          approvers=["group:HR Approvers"]),
        "Sales - Write": dict(description="Edit sales data", owner="tess", entitlements=["Sales-Write"],
                              requestable=False),
        "Payroll Admin": dict(description="Payroll administration", owner="frank", source="Workday",
                              entitlements=["Payroll Admin"], approvers=["manager", "group:HR Approvers"]),
        "Helpdesk Tools": dict(description="Tools for the service desk", owner="gina", entitlements=["Helpdesk"],
                               approvers=["manager"]),
        "IT Admin Access": dict(description="Domain administration", owner="erin", entitlements=["IT-Admins"],
                                approvers=["manager", "group:IT Approvers"]),
        "Cloud Admin": dict(description="Cloud subscription administrators", owner="erin",
                            entitlements=["Cloud-Admins"], approvers=["group:IT Approvers"]),
    },
    PROD: {
        "Finance - Read": dict(description="Finance read", owner="alice", entitlements=["Finance-Read"],
                               approvers=["manager"]),
        "Finance - Write": dict(description="Change finance data", owner="carol", entitlements=["Finance-Write"],
                                approvers=["manager", "group:Finance Approvers"]),
        "HR - Read": dict(description="Read HR folders", owner="carol", entitlements=["HR-Read"],
                          approvers=["group:HR Approvers"]),
        "Sales - Write": dict(description="Sales write access", owner="bob", entitlements=["Sales-Write"]),
        "Payroll Admin": dict(description="Payroll administration", owner="frank", source="Workday",
                              entitlements=["Payroll Admin"], approvers=["manager", "group:HR Approvers"],
                              enabled=False),
        "Legacy VPN": dict(description="", owner="alice", entitlements=["VPN Users"]),
    },
}

ROLES = {  # name: (description, owner, access profiles, requestable)
    TEST: {
        "Finance Analyst": ("Everything a finance analyst needs", "carol", ["Finance - Read", "HR - Read"], True),
        "HR Partner": ("HR business partner", "carol", ["HR - Read"], True),
        "Helpdesk Agent": ("Service desk agent", "gina", ["Helpdesk Tools"], True),
    },
    PROD: {
        "Finance Analyst": ("Finance analyst", "carol", ["Finance - Read"], False),
        "HR Partner": ("HR business partner", "carol", ["HR - Read"], True),
    },
}


# Change history: (type, name, days ago, who, action, {attribute: (old, new)}). The latest entry is also the
# object's last-modified time. In production some objects were changed after test, so moving test's
# values would overwrite a newer production change.
HISTORY = {
    TEST: [
        ("access_profile", "Finance - Read", 12, "alice", "Update access profile",
         {"description": ("Finance read", "Read access to finance shares")}),
        ("access_profile", "Finance - Read", 5, "carol", "Update access profile",
         {"approval_schemes": (["manager"], ["manager", "group Finance Approvers"])}),
        ("access_profile", "Finance - Read", 2, "alice", "Update access profile",
         {"owner": ("Alice Admin", "Carol Owner"), "entitlements": (["Finance-Read"], ["Finance-Read", "VPN Users"])}),
        ("access_profile", "Finance - Write", 10, "carol", "Update access profile", {"comments_required": (False, True)}),
        ("access_profile", "Sales - Write", 7, "tess", "Update access profile",
         {"owner": ("Bob Builder", "Tess Tester"), "requestable": (True, False)}),
        ("source", "Active Directory", 9, "erin", "Update source",
         {"description": ("Corporate Active Directory", "Corporate AD – all employees and contractors")}),
        ("source", "Active Directory", 3, "erin", "Update source",
         {"config.pageSize": ("500", "1000"), "delete_threshold": (10, 20), "config.manageRecycleBin": (False, True)}),
        ("governance_group", "Finance Approvers", 4, "alice", "Add governance group members",
         {"members": (["Alice Admin", "Carol Owner"], ["Alice Admin", "Carol Owner", "Frank Finance"])}),
        ("role", "Finance Analyst", 6, "carol", "Update role",
         {"access_profiles": (["Finance - Read"], ["Finance - Read", "HR - Read"]), "requestable": (False, True)}),
    ],
    PROD: [
        ("access_profile", "Finance - Read", 40, "alice", "Update access profile",
         {"description": ("Finance read access", "Finance read")}),
        ("access_profile", "Finance - Write", 1, "bob", "Update access profile", {"comments_required": (True, False)}),
        ("access_profile", "Payroll Admin", 2, "frank", "Update access profile", {"enabled": (True, False)}),
        ("source", "Active Directory", 60, "alice", "Update source",
         {"description": ("Active Directory", "Corporate Active Directory")}),
        ("source", "Workday", 1, "alice", "Update source", {"owner": ("Carol Owner", "Frank Finance")}),
        ("governance_group", "Finance Approvers", 30, "alice", "Update governance group",
         {"description": ("Approves finance access", "Approves access to finance systems")}),
    ],
}


def _stamp(days_ago, now):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - days_ago * 86400 - 3600 * (days_ago % 7)))


def _id(env, *parts):
    return hashlib.md5("|".join((env,) + parts).encode()).hexdigest()


def seed(flavor):
    """A fresh sandbox state for one side of the demo pair."""
    env = "test" if flavor == TEST else "prod"
    state = {"identities": [], "sources": [], "entitlements": [], "access_profiles": [], "governance_groups": [],
             "roles": [], "access_requests": [], "approvals": []}
    cluster_name = "Test VA cluster" if flavor == TEST else "Prod VA cluster"
    state["clusters"] = [{"id": _id(env, "cluster"), "name": cluster_name}]
    people = {}
    for alias, (name, in_test, in_prod) in PEOPLE.items():
        if in_test if flavor == TEST else in_prod:
            person = {"id": _id(env, "identity", alias), "name": name, "alias": alias,
                      "email": f"{alias}@acme.example"}
            state["identities"].append(person)
            people[alias] = person

    def ident(alias):
        return {"type": "IDENTITY", "id": people[alias]["id"], "name": people[alias]["name"]}

    sources = {}
    dc = "DC=test,DC=acme,DC=local" if flavor == TEST else "DC=acme,DC=local"
    for name, spec in SOURCES[flavor].items():
        spec = dict(spec)
        source = {"id": _id(env, "source", name), "name": name, "owner": ident(spec.pop("owner")),
                  "authoritative": spec.pop("authoritative", False), **spec}
        if source.pop("cluster", False):
            source["cluster"] = {"type": "CLUSTER", "id": state["clusters"][0]["id"], "name": cluster_name}
        state["sources"].append(source)
        sources[name] = source
        extra = TEST_ONLY_ENTITLEMENTS if flavor == TEST else PROD_ONLY_ENTITLEMENTS
        names = ENTITLEMENTS[name] + extra.get(name, [])
        for ent in names:
            state["entitlements"].append({
                "id": _id(env, "entitlement", name, ent), "source_id": source["id"], "name": ent,
                "value": f"CN={ent},OU=Groups,{dc}" if name == "Active Directory" else ent,
                "description": f"Grants {ent}", "owner": ident("alice"),
                "privileged": ent in ("IT-Admins", "Cloud-Admins", "System Administrator", "Payroll Admin", "admin"),
                "requestable": True})
    ents = {(e["source_id"], e["name"]): e for e in state["entitlements"]}

    groups = {}
    for name, (description, owner, members) in GROUPS[flavor].items():
        group = {"id": _id(env, "group", name), "name": name, "description": description, "owner": ident(owner),
                 "members": [people[m]["id"] for m in members]}
        state["governance_groups"].append(group)
        groups[name] = group

    profiles = {}
    for name, p in PROFILES[flavor].items():
        source = sources[p.get("source", "Active Directory")]
        schemes = [{"approverType": "GOVERNANCE_GROUP", "approverId": groups[a[6:]]["id"]} if a.startswith("group:")
                   else {"approverType": a.upper(), "approverId": None} for a in p.get("approvers", [])]
        profile = {
            "id": _id(env, "access_profile", name), "name": name, "description": p["description"] or None,
            "owner": ident(p["owner"]), "source": {"type": "SOURCE", "id": source["id"], "name": source["name"]},
            "entitlements": [{"type": "ENTITLEMENT", "id": ents[(source["id"], e)]["id"], "name": e}
                             for e in p["entitlements"]],
            "enabled": p.get("enabled", True), "requestable": p.get("requestable", True),
            "accessRequestConfig": {"commentsRequired": p.get("comments", False), "denialCommentsRequired": False,
                                    "approvalSchemes": schemes}}
        state["access_profiles"].append(profile)
        profiles[name] = profile

    for name, (description, owner, aps, requestable) in ROLES[flavor].items():
        state["roles"].append({
            "id": _id(env, "role", name), "name": name, "description": description, "owner": ident(owner),
            "accessProfiles": [{"type": "ACCESS_PROFILE", "id": profiles[a]["id"], "name": a} for a in aps],
            "requestable": requestable, "enabled": True})

    now = time.time()
    collections = {"access_profile": "access_profiles", "role": "roles", "governance_group": "governance_groups",
                   "source": "sources"}
    state["events"] = []
    for type_, name, days, who, action, changes in HISTORY[flavor]:
        obj = next(o for o in state[collections[type_]] if o["name"] == name)
        state["events"].append({"time": _stamp(days, now), "actor": PEOPLE[who][0], "action": action, "type": type_,
                                "target_id": obj["id"], "target_name": name,
                                "changes": {f: {"old": old, "new": new} for f, (old, new) in changes.items()}})
    for type_, collection in collections.items():
        for obj in state[collection]:
            obj["created"] = _stamp(120, now)
            mine = [e["time"] for e in state["events"] if e["target_id"] == obj["id"]]
            obj["modified"] = max(mine) if mine else _stamp(45, now)
            state["events"].append({"time": obj["created"], "actor": "Alice Admin",
                                    "action": f"Create {type_.replace('_', ' ')}", "type": type_,
                                    "target_id": obj["id"], "target_name": obj["name"], "changes": {}})
    return state
