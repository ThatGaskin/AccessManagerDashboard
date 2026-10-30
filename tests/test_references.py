"""Reference columns: local validation, batched lookups, contains-search suggestions."""
import re

from amtoolkit.actions import PlanContext, get_action, references
from amtoolkit.connectors.base import ConnectorError
from amtoolkit.connectors.sailpoint import SailPointISC
from amtoolkit.engine import prefetch_references
from tests.test_experimental import items, job
from tests.test_governance_groups import StaticToken
from tests.test_units import FakeResp
from tests.test_workflow import make_job

ALICE = "2c9180835d2e5168015d32f890ca1581"


def test_malformed_ids_fail_before_any_lookup(toolkit, sandbox):
    j = job(toolkit, sandbox, "access_profile.delete", "id,name\nnot-an-id,Finance - Read\n")
    item = items(toolkit, j)[0]
    assert item["op"] == "error"
    assert [m["code"] for m in item["messages"]] == ["invalid_ref"]


def test_free_text_is_accepted_and_misses_suggest_similar_names(toolkit, sandbox):
    csv = (b"name,owner,source,entitlements\n"
           b"AP1,alic,Active Directry,Finance-Read\n")
    item = items(toolkit, make_job(toolkit, sandbox, csv))[0]
    texts = {m["field"]: m["text"] for m in item["messages"] if m["level"] == "error"}
    assert "Did you mean: 'alice@example.com'" in texts["owner"]
    assert "was not found" in texts["source"]  # nothing contains the typo, so no hint


def test_duplicate_values_in_a_list_warn(toolkit, sandbox):
    j = job(toolkit, sandbox, "governance_group.create", "name,owner,members\nTeam X,alice,bob;BOB\n")
    item = items(toolkit, j)[0]
    assert any(m["code"] == "duplicate_ref" for m in item["messages"])


def test_search_endpoint_uses_contains(client, sandbox):
    rows = client.get(f"/api/connections/{sandbox['id']}/search?kind=source&q=direct").get_json()
    assert [r["value"] for r in rows] == ["Active Directory"]
    rows = client.get(f"/api/connections/{sandbox['id']}/search?kind=identity&q=example").get_json()
    assert len(rows) == 3
    rows = client.get(f"/api/connections/{sandbox['id']}/search?kind=entitlement&q=read&scope=Active Directory")
    assert all("Read" in r["value"] for r in rows.get_json())
    assert client.get(f"/api/connections/{sandbox['id']}/search?kind=secrets&q=xx").status_code == 400


class Counting:
    """Connector stub that counts calls and answers like the sandbox would."""

    def __init__(self):
        self.calls = []

    def find_many(self, kind, values, scope=None):
        self.calls.append(("find_many", kind, tuple(values)))
        return {v.lower(): [{"id": ALICE, "name": "Alice"}] for v in values}

    def find_identities(self, value):
        self.calls.append(("find_identities", value))
        return [{"id": ALICE, "name": "Alice"}]


def test_prefetch_batches_and_ignores_case():
    action = get_action("governance_group.create")
    ctx = PlanContext(Counting())
    rows = [{"name": "G1", "owner": "Alice@Example.com", "members": "bob;carol"},
            {"name": "G2", "owner": "alice@example.com", "members": "BOB"}]
    prefetch_references(action, ctx, [{"input": r} for r in rows])
    kinds = [c[1] for c in ctx.connector.calls]
    assert kinds.count("identity") == 1 and kinds.count("governance_group") == 1
    identity_call = next(c for c in ctx.connector.calls if c[1] == "identity")
    assert sorted(v.lower() for v in identity_call[2]) == ["alice@example.com", "bob", "carol"]
    before = len(ctx.connector.calls)
    for row in rows:
        action.plan(ctx, row, {"if_exists": "skip"})
    assert len(ctx.connector.calls) == before  # every lookup was served from the prefetch
    assert ctx.has(("identity", ALICE))  # also reachable by ID


class TenantSession:
    """Fake ISC: answers `field in (…)`/`eq`/`co`/`sw` filters over a small identity list; can reject operators."""

    def __init__(self, reject=()):
        self.reject = set(reject)
        self.calls = []
        self.people = [{"id": f"{n:032x}", "name": f"Person {n}", "alias": f"user{n}", "email": f"user{n}@corp.com",
                        "firstname": "Person", "lastname": str(n)} for n in range(1, 121)]

    def request(self, method, url, params=None, **kw):
        flt = (params or {}).get("filters", "")
        self.calls.append(flt)
        m = re.match(r'(\w+) (in|eq|co|sw) (.*)', flt)
        field, op, rest = m.groups()
        if op in self.reject:
            return FakeResp(400, {"message": f"operator {op} not supported"})
        values = [v.lower() for v in re.findall(r'"([^"]*)"', rest)]
        test = {"in": lambda x: x in values, "eq": lambda x: x == values[0], "co": lambda x: values[0] in x,
                "sw": lambda x: x.startswith(values[0])}[op]
        found = [p for p in self.people if test(str(p.get(field, "")).lower())]
        return FakeResp(200, found[:params.get("limit", 250)])


def isc(session):
    return SailPointISC({"base_url": "https://acme.api.identitynow.com"}, None, token=StaticToken(), session=session)


def test_sailpoint_identities_are_looked_up_fifty_per_request():
    session = TenantSession()
    wanted = [f"user{n}@corp.com" for n in range(1, 101)] + ["nobody@corp.com"]
    found = isc(session).find_many("identity", wanted)
    assert len(session.calls) == 3  # 101 emails, 50 per request
    assert found["user7@corp.com"][0]["name"] == "Person 7" and found["nobody@corp.com"] == []


def test_sailpoint_batch_falls_back_when_in_is_rejected():
    session = TenantSession(reject={"in"})
    connector = isc(session)
    assert connector.find_many("identity", ["user1", "user2"]) == {}  # left to single lookups
    assert connector.find_many("identity", ["user3"]) == {}
    assert len(session.calls) == 1  # the rejection is remembered


def test_sailpoint_search_uses_contains_and_falls_back_to_starts_with():
    session = TenantSession()
    rows = isc(session).suggest("identity", "ser11")
    assert {r["value"] for r in rows} >= {"user11@corp.com", "user110@corp.com"}
    assert any(c.startswith("alias co") for c in session.calls)
    session = TenantSession(reject={"co"})
    connector = isc(session)
    assert {r["value"] for r in connector.suggest("identity", "user11")} >= {"user11@corp.com"}
    assert any(c.startswith("alias sw") for c in session.calls)


def test_check_value_rejects_only_what_can_never_match():
    assert references.check_value("identity", "any free text, even odd@", "owner") is None
    assert references.check_value("id", "abc", "id")["code"] == "invalid_ref"
    assert references.check_value("source", "two\nlines", "source")["code"] == "invalid_ref"
    assert references.check_value("source", "x" * 300, "source")["code"] == "invalid_ref"


def test_connector_errors_during_prefetch_are_not_fatal():
    class Failing(Counting):
        def find_many(self, kind, values, scope=None):
            raise ConnectorError("HTTP 500: boom", status=500)
    ctx = PlanContext(Failing())
    assert references.prefetch(ctx, [("identity", "alice", None)]) == 0
