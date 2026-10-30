"""SailPoint Identity Security Cloud (IdentityNow) connector.

Uses a Personal Access Token or API client (OAuth2 client credentials). Grant it only the scopes the
actions need, e.g. `idn:access-profile:manage`, `idn:sources:read`, `idn:entitlement:read`,
`idn:identity:read`, and `idn:workgroup:read` / `idn:workgroup:write` for governance groups.
The experimental features additionally use roles, entitlement updates, sources, access requests and
approvals (e.g. `idn:role-unchecked:manage`, `idn:entitlement:update`, `idn:sources:manage`,
`idn:access-request:manage`, `idn:access-request-administration:write`).
"""
import re

from . import model
from .base import ConfigError, Connector, ConnectorError, Field, Interrupted
from .http import ApiClient, ClientCredentialsToken

PATCH_HEADERS = {"Content-Type": "application/json-patch+json"}
PAGE = 250

ID_RE = re.compile(r"^[0-9a-f]{32}$", re.I)


def q(value: str) -> str:
    """Quotes a value for SailPoint `filters` syntax."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def ref(obj, type_):
    return {"type": type_, "id": obj.get("id"), "name": obj.get("name")}


class SailPointISC(Connector):
    kind = "sailpoint_isc"
    label = "SailPoint Identity Security Cloud"
    description = "Calls the ISC REST API (v3) with an API client / personal access token."
    fields = [
        Field("base_url", "API base URL", required=True, default="https://{tenant}.api.identitynow.com",
              help="Your tenant's API host, e.g. https://acme.api.identitynow.com"),
        Field("client_id", "Client ID", required=True),
        Field("secret_env", "Secret environment variable", default="AMT_CLIENT_SECRET",
              help="If this environment variable is set when the toolkit starts, its value is used as the "
                   "client secret. Otherwise enter the secret in the UI; it is kept in memory only."),
        Field("entitlements_api", "Entitlements API version", default="beta",
              help="Path segment for the entitlements endpoint (beta, v2024, v2025 …).",
              choices=["beta", "v2024", "v2025", "v3"]),
        Field("workgroups_api", "Governance groups API version", default="beta",
              help="Path segment for the governance groups (workgroups) endpoint.",
              choices=["beta", "v2024", "v2025"]),
        Field("max_calls_per_10s", "Rate limit (calls / 10 s)", default="80",
              help="Client-side throttle. ISC allows roughly 100 requests per 10 seconds per token."),
    ]

    def __init__(self, config, secret_provider, *, token=None, limiter=None, abort_event=None, session=None):
        base = (config.get("base_url") or "").strip().rstrip("/")
        if not base or "{tenant}" in base:
            raise ConfigError("Set the API base URL of your tenant")
        self.config = config
        self.token = token or ClientCredentialsToken(f"{base}/oauth/token", config.get("client_id"),
                                                     secret_provider)
        self.api = ApiClient(base, self.token, limiter=limiter, abort_event=abort_event, session=session)
        self.ent_api = (config.get("entitlements_api") or "beta").strip("/")
        self.wg_api = (config.get("workgroups_api") or "beta").strip("/")
        # Versioned endpoints that are still experimental in a given release require this header.
        self.wg_headers = {} if self.wg_api == "beta" else {"X-SailPoint-Experimental": "true"}

    def _list(self, path, filters, limit=10, headers=None):
        return self.api.request("GET", path, params={"filters": filters, "limit": limit}, headers=headers) or []

    def test(self):
        self.token.get(force=True)
        self._list("/v3/sources", 'name sw "a"', limit=1)
        return "Authenticated and able to read sources"

    def find_identities(self, value):
        value = value.strip()
        if ID_RE.match(value):
            filters = f"id eq {q(value)}"
        elif "@" in value:
            filters = f"email eq {q(value)}"
        else:
            filters = f"alias eq {q(value)}"
        return [{"id": i["id"], "name": i.get("name"), "email": i.get("email")}
                for i in self._list("/v3/public-identities", filters)]

    def find_sources(self, name):
        return [{"id": s["id"], "name": s["name"]} for s in self._list("/v3/sources", f"name eq {q(name)}")]

    def find_entitlements(self, source_id, value):
        path = f"/{self.ent_api}/entitlements"
        base = f"source.id eq {q(source_id)}"
        found = self._list(path, f"{base} and name eq {q(value)}")
        if not found:
            found = self._list(path, f"{base} and value eq {q(value)}")
        return [{"id": e["id"], "name": e.get("name"), "value": e.get("value")} for e in found]

    def find_access_profiles(self, name):
        return [{"id": p["id"], "name": p["name"], "source": (p.get("source") or {}).get("name")}
                for p in self._list("/v3/access-profiles", f"name eq {q(name)}")]

    def get_access_profile(self, profile_id):
        try:
            return self.api.request("GET", f"/v3/access-profiles/{profile_id}")
        except ConnectorError as exc:
            if exc.status == 404:
                return None
            raise

    def create_access_profile(self, payload):
        return self.api.request("POST", "/v3/access-profiles", json=payload)

    def delete_access_profile(self, profile_id):
        self.api.request("DELETE", f"/v3/access-profiles/{profile_id}")

    # ---------------------------------------------------------------- governance groups ("workgroups")
    @staticmethod
    def _group(g):
        owner = g.get("owner") or {}
        return {"id": g["id"], "name": g.get("name"), "owner": owner.get("displayName") or owner.get("name"),
                "member_count": g.get("memberCount")}

    def find_governance_groups(self, value):
        value = value.strip()
        if ID_RE.match(value) or re.match(r"^[0-9a-f-]{36}$", value, re.I):
            try:
                g = self.api.request("GET", f"/{self.wg_api}/workgroups/{value}", headers=self.wg_headers)
            except ConnectorError as exc:
                if exc.status != 404:
                    raise
            else:
                return [self._group(g)] if g else []
        found = self._list(f"/{self.wg_api}/workgroups", f"name eq {q(value)}", headers=self.wg_headers)
        return [self._group(g) for g in found if (g.get("name") or "").lower() == value.lower()]

    def create_governance_group(self, payload):
        return self.api.request("POST", f"/{self.wg_api}/workgroups", json=payload, headers=self.wg_headers)

    def add_governance_group_members(self, group_id, identity_ids):
        if not identity_ids:
            return []
        body = [{"type": "IDENTITY", "id": i} for i in identity_ids]
        result = self.api.request("POST", f"/{self.wg_api}/workgroups/{group_id}/members/bulk-add", json=body,
                                  headers=self.wg_headers)
        problems = []
        for entry in result if isinstance(result, list) else []:
            status = entry.get("status") if isinstance(entry, dict) else None
            if status and int(status) not in (200, 201, 204, 409):  # 409 = already a member
                problems.append(f"{entry.get('id')}: HTTP {status} {entry.get('description') or ''}".strip())
        return problems

    def delete_governance_group(self, group_id):
        self.api.request("DELETE", f"/{self.wg_api}/workgroups/{group_id}", headers=self.wg_headers)

    # ---------------------------------------------------------------- generic objects (experimental)
    def _endpoint(self, type_):
        ent_headers = {} if self.ent_api in ("beta", "v3") else {"X-SailPoint-Experimental": "true"}
        return {
            "access_profile": ("/v3/access-profiles", {}),
            "role": ("/v3/roles", {}),
            "source": ("/v3/sources", {}),
            "entitlement": (f"/{self.ent_api}/entitlements", ent_headers),
            "governance_group": (f"/{self.wg_api}/workgroups", self.wg_headers),
        }[type_]

    def get_object(self, type_, object_id):
        path, headers = self._endpoint(type_)
        try:
            obj = self.api.request("GET", f"{path}/{object_id}", headers=headers)
        except ConnectorError as exc:
            if exc.status in (400, 404):
                return None
            raise
        return model.normalize(type_, obj) if obj else None

    def find_objects(self, type_, ref, source_id=None):
        ref = ref.strip()
        if ID_RE.match(ref) or re.match(r"^[0-9a-f-]{36}$", ref, re.I):
            found = self.get_object(type_, ref)
            if found:
                return [found]
        path, headers = self._endpoint(type_)
        scope = f"source.id eq {q(source_id)} and " if source_id and type_ == "entitlement" else ""
        items = self._list(path, f"{scope}name eq {q(ref)}", headers=headers)
        if not items and type_ == "entitlement":
            items = self._list(path, f"{scope}value eq {q(ref)}", headers=headers)
        return [model.normalize(type_, o) for o in items]

    def list_objects(self, type_, limit=20000, abort_event=None):
        path, headers = self._endpoint(type_)
        offset = 0
        while offset < limit:
            if abort_event is not None and abort_event.is_set():
                raise Interrupted()
            page = self.api.request("GET", path, params={"offset": offset, "limit": PAGE}, headers=headers) or []
            for obj in page:
                yield model.normalize(type_, obj)
            if len(page) < PAGE:
                return
            offset += PAGE

    def patch_object(self, type_, object_id, changes):
        path, headers = self._endpoint(type_)
        self.api.request("PATCH", f"{path}/{object_id}", json=model.to_patch(changes),
                         headers={**headers, **PATCH_HEADERS})

    def create_role(self, payload):
        return self.api.request("POST", "/v3/roles", json=payload)

    def delete_role(self, role_id):
        self.api.request("DELETE", f"/v3/roles/{role_id}")

    def list_governance_group_members(self, group_id):
        members, offset = [], 0
        while True:
            page = self.api.request("GET", f"/{self.wg_api}/workgroups/{group_id}/members",
                                    params={"offset": offset, "limit": PAGE}, headers=self.wg_headers) or []
            members += [{"id": m["id"], "name": m.get("name") or m.get("displayName")} for m in page]
            if len(page) < PAGE:
                return members
            offset += PAGE

    def remove_governance_group_members(self, group_id, identity_ids):
        if not identity_ids:
            return []
        result = self.api.request("POST", f"/{self.wg_api}/workgroups/{group_id}/members/bulk-delete",
                                  json=[{"type": "IDENTITY", "id": i} for i in identity_ids], headers=self.wg_headers)
        problems = []
        for entry in result if isinstance(result, list) else []:
            status = entry.get("status") if isinstance(entry, dict) else None
            if status and int(status) not in (200, 204, 404):
                problems.append(f"{entry.get('id')}: HTTP {status} {entry.get('description') or ''}".strip())
        return problems

    @staticmethod
    def _request_status(r):
        obj = r.get("requestedObject") or {}
        return {"id": r.get("accessRequestId") or r.get("id"), "state": r.get("state"),
                "request_type": r.get("requestType"), "item_id": obj.get("id"), "item_name": obj.get("name"),
                "requested_for": (r.get("requestedFor") or {}).get("id")}

    def find_access_requests(self, identity_id):
        rows = self.api.request("GET", "/v3/access-request-status",
                                params={"requested-for": identity_id, "limit": PAGE}) or []
        return [self._request_status(r) for r in rows if r.get("state") in ("EXECUTING", "PENDING", None)]

    def get_access_request(self, request_id):
        rows = self.api.request("GET", "/v3/access-request-status",
                                params={"filters": f"accessRequestId eq {q(request_id)}", "limit": 1}) or []
        return self._request_status(rows[0]) if rows else None

    def submit_access_request(self, payload):
        return self.api.request("POST", "/v3/access-requests", json=payload) or {}

    def cancel_access_request(self, request_id, comment):
        self.api.request("POST", "/v3/access-requests/cancel", json={"accountActivityId": request_id,
                                                                     "comment": comment})

    def get_pending_approval(self, approval_id):
        rows = self.api.request("GET", "/v3/access-request-approvals/pending",
                                params={"filters": f"id eq {q(approval_id)}", "limit": 1}) or []
        if not rows:
            return None
        a = rows[0]
        owner = a.get("owner") or {}
        return {"id": a["id"], "approver_id": owner.get("id"), "approver_name": owner.get("name"),
                "requested_for": (a.get("requestedFor") or {}).get("name"),
                "item_name": (a.get("requestedObject") or {}).get("name")}

    def forward_approval(self, approval_id, new_owner_id, comment):
        self.api.request("POST", f"/v3/access-request-approvals/{approval_id}/forward",
                         json={"newOwnerId": new_owner_id, "comment": comment})
