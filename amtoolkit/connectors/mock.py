"""Offline sandbox tenant. Lets you rehearse a whole bulk job — including pause, resume, failures and
rollback — without touching a real system. State is saved to a JSON file in the data directory."""
import json
import random
import threading
import time
import uuid
from pathlib import Path

from . import model
from .base import Connector, ConnectorError, Field, Interrupted

SEED = {
    "identities": [
        {"id": "2c9180835d2e5168015d32f890ca1581", "name": "Alice Admin", "alias": "alice", "email": "alice@example.com"},
        {"id": "2c9180835d2e5168015d32f890ca1582", "name": "Bob Builder", "alias": "bob", "email": "bob@example.com"},
        {"id": "2c9180835d2e5168015d32f890ca1583", "name": "Carol Owner", "alias": "carol", "email": "carol@example.com"},
    ],
    "sources": [
        {"id": "2c91808568c529c60168cca6f90c1301", "name": "Active Directory"},
        {"id": "2c91808568c529c60168cca6f90c1302", "name": "Workday"},
        {"id": "2c91808568c529c60168cca6f90c1303", "name": "Salesforce"},
    ],
    "entitlements": [],
    "access_profiles": [],
    "governance_groups": [
        {"id": "2c9180867a4e5b2a017a4f1d2b3c0001", "name": "Finance Approvers", "description": "Approves finance access",
         "owner": {"type": "IDENTITY", "id": "2c9180835d2e5168015d32f890ca1581", "name": "Alice Admin"},
         "members": ["2c9180835d2e5168015d32f890ca1581", "2c9180835d2e5168015d32f890ca1583"]},
    ],
}
for _src, _names in (("2c91808568c529c60168cca6f90c1301",
                      ["Domain Users", "VPN Users", "Finance-Read", "Finance-Write", "HR-Read", "HR-Write",
                       "Sales-Read", "Sales-Write", "IT-Admins", "Helpdesk"]),
                     ("2c91808568c529c60168cca6f90c1302", ["Employee", "Manager", "HR Partner", "Payroll Admin"]),
                     ("2c91808568c529c60168cca6f90c1303", ["Standard User", "Sales Manager", "System Administrator",
                                                            "Read Only"])):
    for _i, _name in enumerate(_names):
        SEED["entitlements"].append({"id": f"{_src[:24]}{len(SEED['entitlements']):08x}", "source_id": _src,
                                     "name": _name, "value": f"CN={_name},OU=Groups" if _src.endswith("1301") else _name})

# Extra seed data so the experimental features and reports have something to work on.
_ALICE = {"type": "IDENTITY", "id": "2c9180835d2e5168015d32f890ca1581", "name": "Alice Admin"}
_GONE = {"type": "IDENTITY", "id": "2c9180835d2e5168015d32f8900dead0", "name": "Former Employee"}
for _e in SEED["entitlements"]:
    _e.update(description="" if _e["name"].endswith("Write") or _e["name"] == "IT-Admins" else f"Grants {_e['name']}",
              owner=None if _e["name"] in ("IT-Admins", "Payroll Admin") else dict(_ALICE),
              privileged=_e["name"] in ("IT-Admins", "System Administrator", "Payroll Admin"), requestable=True)
for _s in SEED["sources"]:
    _s["owner"] = dict(_GONE) if _s["name"] == "Workday" else dict(_ALICE)
SEED["access_profiles"].append({
    "id": "8f1e2d3c4b5a69788796a5b4c3d2e1f0", "name": "Legacy VPN", "description": None, "owner": dict(_GONE),
    "source": {"type": "SOURCE", "id": "2c91808568c529c60168cca6f90c1301", "name": "Active Directory"},
    "entitlements": [{"type": "ENTITLEMENT", "id": SEED["entitlements"][1]["id"], "name": "VPN Users"}],
    "enabled": True, "requestable": True,
    "accessRequestConfig": {"commentsRequired": False, "denialCommentsRequired": False, "approvalSchemes": []}})
SEED["roles"] = []
SEED["access_requests"] = []
SEED["approvals"] = [
    {"id": "a1b2c3d4e5f60718293a4b5c6d7e8f90", "owner_id": "2c9180835d2e5168015d32f890ca1582",
     "requested_for": "Carol Owner", "item_name": "Finance - Read", "state": "PENDING"},
    {"id": "a1b2c3d4e5f60718293a4b5c6d7e8f91", "owner_id": "2c9180835d2e5168015d32f890ca1582",
     "requested_for": "Alice Admin", "item_name": "Sales - Manager", "state": "PENDING"},
]
COLLECTIONS = {"access_profile": "access_profiles", "role": "roles", "entitlement": "entitlements",
               "source": "sources", "governance_group": "governance_groups"}

_lock = threading.Lock()


class MockTenant(Connector):
    kind = "mock"
    label = "Sandbox (offline)"
    description = "Simulated tenant stored on disk. Safe for practice and demos."
    needs_secret = False
    fields = [
        Field("latency_ms", "Simulated latency (ms)", default="250"),
        Field("failure_rate", "Simulated failure rate (0–1)", default="0.05",
              help="Fraction of create/delete calls that fail with a retryable error."),
    ]

    def __init__(self, config, state_path: Path, abort_event=None):
        self.config = config
        self.path = Path(state_path)
        self.abort_event = abort_event
        self.latency = float(config.get("latency_ms") or 0) / 1000
        self.failure_rate = float(config.get("failure_rate") or 0)

    def _state(self):
        state = json.loads(self.path.read_text("utf-8")) if self.path.exists() else json.loads(json.dumps(SEED))
        for key, value in SEED.items():  # sandboxes created by older versions lack newer collections
            state.setdefault(key, json.loads(json.dumps(value)))
        return state

    def _save(self, state):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), "utf-8")
        tmp.replace(self.path)

    def _delay(self):
        if self.latency:
            if self.abort_event is not None and self.abort_event.wait(self.latency * random.uniform(0.5, 1.5)):
                raise Interrupted()
            elif self.abort_event is None:
                time.sleep(self.latency)

    def _maybe_fail(self):
        if self.failure_rate and random.random() < self.failure_rate:
            raise ConnectorError("HTTP 503: simulated outage", status=503, retryable=True)

    def test(self):
        with _lock:
            state = self._state()
        return (f"Sandbox ready: {len(state['identities'])} identities, {len(state['sources'])} sources, "
                f"{len(state['access_profiles'])} access profiles, {len(state['governance_groups'])} governance groups")

    def find_identities(self, value):
        self._delay()
        v = value.strip().lower()
        with _lock:
            return [{"id": i["id"], "name": i["name"], "email": i["email"]} for i in self._state()["identities"]
                    if v in (i["id"].lower(), i["alias"].lower(), i["email"].lower())]

    def find_sources(self, name):
        self._delay()
        with _lock:
            return [dict(s) for s in self._state()["sources"] if s["name"].lower() == name.strip().lower()]

    def find_entitlements(self, source_id, value):
        self._delay()
        v = value.strip().lower()
        with _lock:
            ents = [e for e in self._state()["entitlements"] if e["source_id"] == source_id]
        found = [e for e in ents if e["name"].lower() == v] or [e for e in ents if e["value"].lower() == v]
        return [{"id": e["id"], "name": e["name"], "value": e["value"]} for e in found]

    def find_access_profiles(self, name):
        self._delay()
        with _lock:
            return [{"id": p["id"], "name": p["name"], "source": p["source"]["name"]}
                    for p in self._state()["access_profiles"] if p["name"].lower() == name.strip().lower()]

    def get_access_profile(self, profile_id):
        self._delay()
        with _lock:
            return next((p for p in self._state()["access_profiles"] if p["id"] == profile_id), None)

    def create_access_profile(self, payload):
        self._delay()
        self._maybe_fail()
        with _lock:
            state = self._state()
            if any(p["name"].lower() == payload["name"].lower() for p in state["access_profiles"]):
                raise ConnectorError(f"HTTP 400: Access profile '{payload['name']}' already exists", status=400)
            profile = dict(payload, id=uuid.uuid4().hex, created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            state["access_profiles"].append(profile)
            self._save(state)
        return profile

    def delete_access_profile(self, profile_id):
        self._delay()
        self._maybe_fail()
        with _lock:
            state = self._state()
            before = len(state["access_profiles"])
            state["access_profiles"] = [p for p in state["access_profiles"] if p["id"] != profile_id]
            if len(state["access_profiles"]) == before:
                raise ConnectorError("Not found", status=404)
            self._save(state)

    def find_governance_groups(self, value):
        self._delay()
        v = value.strip().lower()
        with _lock:
            return [{"id": g["id"], "name": g["name"], "owner": g["owner"].get("name"), "member_count": len(g["members"])}
                    for g in self._state()["governance_groups"] if v in (g["id"].lower(), g["name"].lower())]

    def create_governance_group(self, payload):
        self._delay()
        self._maybe_fail()
        with _lock:
            state = self._state()
            if any(g["name"].lower() == payload["name"].lower() for g in state["governance_groups"]):
                raise ConnectorError(f"HTTP 400: Governance group '{payload['name']}' already exists", status=400)
            group = dict(payload, id=uuid.uuid4().hex, members=[])
            state["governance_groups"].append(group)
            self._save(state)
        return group

    def add_governance_group_members(self, group_id, identity_ids):
        self._delay()
        with _lock:
            state = self._state()
            group = next((g for g in state["governance_groups"] if g["id"] == group_id), None)
            if group is None:
                raise ConnectorError("Not found", status=404)
            known = {i["id"] for i in state["identities"]}
            problems = [f"{i}: HTTP 404 identity not found" for i in identity_ids if i not in known]
            group["members"] = sorted(set(group["members"]) | ({*identity_ids} & known))
            self._save(state)
        return problems

    def delete_governance_group(self, group_id):
        self._delay()
        self._maybe_fail()
        with _lock:
            state = self._state()
            before = len(state["governance_groups"])
            state["governance_groups"] = [g for g in state["governance_groups"] if g["id"] != group_id]
            if len(state["governance_groups"]) == before:
                raise ConnectorError("Not found", status=404)
            self._save(state)

    # ---------------------------------------------------------------- generic objects (experimental)
    @staticmethod
    def _view(type_, obj, state):
        view = dict(obj)
        if type_ == "entitlement":
            src = next((x for x in state["sources"] if x["id"] == obj["source_id"]), {})
            view["source"] = {"type": "SOURCE", "id": obj["source_id"], "name": src.get("name")}
        if type_ == "governance_group":
            view["memberCount"] = len(obj.get("members", []))
        return model.normalize(type_, view)

    def get_object(self, type_, object_id):
        self._delay()
        with _lock:
            state = self._state()
            obj = next((o for o in state[COLLECTIONS[type_]] if o["id"] == object_id), None)
            return self._view(type_, obj, state) if obj else None

    def find_objects(self, type_, ref, source_id=None):
        self._delay()
        v = ref.strip().lower()
        with _lock:
            state = self._state()
            objs = [o for o in state[COLLECTIONS[type_]] if not source_id or o.get("source_id", source_id) == source_id]
            found = [o for o in objs if o["id"].lower() == v or (o.get("name") or "").lower() == v]
            if not found and type_ == "entitlement":
                found = [o for o in objs if (o.get("value") or "").lower() == v]
            return [self._view(type_, o, state) for o in found]

    def list_objects(self, type_, limit=20000, abort_event=None):
        self._delay()
        with _lock:
            state = self._state()
            objs = [self._view(type_, o, state) for o in state[COLLECTIONS[type_]][:limit]]
        yield from objs

    def patch_object(self, type_, object_id, changes):
        self._delay()
        self._maybe_fail()
        with _lock:
            state = self._state()
            obj = next((o for o in state[COLLECTIONS[type_]] if o["id"] == object_id), None)
            if obj is None:
                raise ConnectorError("Not found", status=404)
            ops = model.to_patch(changes)
            for op in ops:
                if op["path"] == "/owner":
                    ident = next((i for i in state["identities"] if i["id"] == op["value"]["id"]), None)
                    if ident is None:
                        raise ConnectorError("HTTP 400: owner identity not found", status=400)
                    op["value"]["name"] = ident["name"]
            model.apply_patch(obj, ops)
            self._save(state)

    def create_role(self, payload):
        self._delay()
        self._maybe_fail()
        with _lock:
            state = self._state()
            if any(r["name"].lower() == payload["name"].lower() for r in state["roles"]):
                raise ConnectorError(f"HTTP 400: Role '{payload['name']}' already exists", status=400)
            role = dict(payload, id=uuid.uuid4().hex)
            state["roles"].append(role)
            self._save(state)
        return role

    def delete_role(self, role_id):
        self._delay()
        with _lock:
            state = self._state()
            before = len(state["roles"])
            state["roles"] = [r for r in state["roles"] if r["id"] != role_id]
            if len(state["roles"]) == before:
                raise ConnectorError("Not found", status=404)
            self._save(state)

    def list_governance_group_members(self, group_id):
        self._delay()
        with _lock:
            state = self._state()
            group = next((g for g in state["governance_groups"] if g["id"] == group_id), None)
            if group is None:
                raise ConnectorError("Not found", status=404)
            names = {i["id"]: i["name"] for i in state["identities"]}
            return [{"id": m, "name": names.get(m)} for m in group["members"]]

    def remove_governance_group_members(self, group_id, identity_ids):
        self._delay()
        with _lock:
            state = self._state()
            group = next((g for g in state["governance_groups"] if g["id"] == group_id), None)
            if group is None:
                raise ConnectorError("Not found", status=404)
            group["members"] = [m for m in group["members"] if m not in set(identity_ids)]
            self._save(state)
        return []

    def find_access_requests(self, identity_id):
        self._delay()
        with _lock:
            return [dict(r) for r in self._state()["access_requests"]
                    if r["requested_for"] == identity_id and r["state"] == "PENDING"]

    def get_access_request(self, request_id):
        self._delay()
        with _lock:
            return next((dict(r) for r in self._state()["access_requests"] if r["id"] == request_id), None)

    def submit_access_request(self, payload):
        self._delay()
        self._maybe_fail()
        with _lock:
            state = self._state()
            created = []
            for who in payload["requestedFor"]:
                for item in payload["requestedItems"]:
                    if any(r["requested_for"] == who and r["item_id"] == item["id"] and r["state"] == "PENDING"
                           for r in state["access_requests"]):
                        raise ConnectorError("HTTP 400: an access request for this item is already pending",
                                             status=400)
                    req = {"id": uuid.uuid4().hex, "state": "PENDING", "request_type": payload["requestType"],
                           "item_id": item["id"], "item_name": item.get("name"), "requested_for": who}
                    state["access_requests"].append(req)
                    created.append(req)
            self._save(state)
        return {"newRequests": created}

    def cancel_access_request(self, request_id, comment):
        self._delay()
        with _lock:
            state = self._state()
            req = next((r for r in state["access_requests"] if r["id"] == request_id), None)
            if req is None or req["state"] != "PENDING":
                raise ConnectorError("HTTP 400: request is not pending", status=400)
            req["state"] = "CANCELLED"
            self._save(state)

    def get_pending_approval(self, approval_id):
        self._delay()
        with _lock:
            state = self._state()
            a = next((a for a in state["approvals"] if a["id"] == approval_id and a["state"] == "PENDING"), None)
            if a is None:
                return None
            names = {i["id"]: i["name"] for i in state["identities"]}
            return {"id": a["id"], "approver_id": a["owner_id"], "approver_name": names.get(a["owner_id"]),
                    "requested_for": a["requested_for"], "item_name": a["item_name"]}

    def forward_approval(self, approval_id, new_owner_id, comment):
        self._delay()
        self._maybe_fail()
        with _lock:
            state = self._state()
            a = next((a for a in state["approvals"] if a["id"] == approval_id and a["state"] == "PENDING"), None)
            if a is None:
                raise ConnectorError("HTTP 404: approval not found", status=404)
            a["owner_id"] = new_owner_id
            self._save(state)
