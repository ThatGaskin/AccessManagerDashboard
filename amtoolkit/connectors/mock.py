"""Offline sandbox tenant. Lets you rehearse a whole bulk job — including pause, resume, failures and
rollback — without touching a real system. State is saved to a JSON file in the data directory."""
import json
import random
import threading
import time
import uuid
from pathlib import Path

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
        if self.path.exists():
            return json.loads(self.path.read_text("utf-8"))
        return json.loads(json.dumps(SEED))

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
                f"{len(state['access_profiles'])} access profiles")

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
