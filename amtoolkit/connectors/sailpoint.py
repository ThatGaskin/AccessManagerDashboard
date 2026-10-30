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
CHUNK = 50  # values per batched "in (…)" filter, keeping URLs well below server limits

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
        self._no_contains = set()  # (path, field) pairs where the tenant rejected a "co" filter
        self._no_bulk = set()  # (kind, field) pairs this tenant rejected in an "in (…)" filter

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

    def search_identities(self, query, limit=20):
        query = query.strip()
        if ID_RE.match(query):
            return self.find_identities(query)
        # Partial text: "contains" on each field. Run simple filters one by one rather than one OR-filter, so a tenant that rejects one of them
        # (unsupported operator) still returns results from the others.
        found = {}
        fields = ("email", "alias") if "@" in query else ("alias", "firstname", "lastname", "email")
        for field in fields:
            try:
                rows = self._contains("/v3/public-identities", field, query, limit)
            except ConnectorError as exc:
                if exc.status == 400:
                    continue
                raise
            for i in rows:
                found.setdefault(i["id"], {"id": i["id"], "name": i.get("name"), "email": i.get("email"),
                                           "alias": i.get("alias")})
        return sorted(found.values(), key=lambda i: (i["name"] or "").lower())[:limit]

    def _contains(self, path, field, text, limit, *, scope="", headers=None):
        """`field co "text"`, or `sw` where the tenant does not support `co` for that field (HTTP 400)."""
        ops = ["sw"] if (path, field) in self._no_contains else ["co", "sw"]
        for op in ops:
            try:
                return self._list(path, f"{scope}{field} {op} {q(text)}", limit=limit, headers=headers)
            except ConnectorError as exc:
                if exc.status != 400 or op == "sw":
                    raise
                self._no_contains.add((path, field))
        return []

    def suggest(self, kind, text, scope=None, limit=10):
        text = text.strip()
        if not text:
            return []
        if kind == "identity":
            return [{"value": i.get("email") or i.get("alias") or i["id"],
                     "label": f"{i.get('name') or i['id']} ({i.get('email') or i.get('alias') or i['id']})"}
                    for i in self.search_identities(text, limit)]
        type_ = kind.split(":", 1)[-1]
        path, headers = self._endpoint(type_)
        scoped = f"source.id eq {q(scope)} and " if scope and type_ == "entitlement" else ""
        try:
            rows = self._contains(path, "name", text, limit, scope=scoped, headers=headers)
        except ConnectorError as exc:
            if exc.status == 400:
                return []
            raise
        return [{"value": o.get("name"), "label": o.get("name")} for o in rows if o.get("name")]

    # ---------------------------------------------------------------- batched lookups
    def _list_in(self, path, field, values, *, scope="", headers=None):
        """Objects whose `field` is one of `values`, CHUNK values per request. Returns (objects, complete);
        complete is False when a page was full, i.e. some matches may be missing."""
        objects, complete = [], True
        for i in range(0, len(values), CHUNK):
            chunk = values[i:i + CHUNK]
            found = self._list(path, f"{scope}{field} in ({','.join(q(v) for v in chunk)})", limit=PAGE,
                               headers=headers)
            objects += found
            complete = complete and len(found) < PAGE
        return objects, complete

    def _bulk(self, kind, values, path, fields, *, scope="", headers=None, shape=lambda o: o):
        """Batched lookup by one or more fields. A field the tenant cannot filter with `in` (HTTP 400) is
        remembered and left to single lookups."""
        result = {}
        remaining = list(values)
        for field in fields:
            if not remaining or (kind, field) in self._no_bulk:
                continue
            try:
                objects, complete = self._list_in(path, field, remaining, scope=scope, headers=headers)
            except ConnectorError as exc:
                if exc.status != 400:
                    raise
                self._no_bulk.add((kind, field))
                continue
            attr, wanted = field.split(".")[-1], {v.lower() for v in remaining}
            for o in objects:
                key = str(o.get(attr) or "").lower()
                if key in wanted:
                    result.setdefault(key, []).append(shape(o))
            if not complete:
                return result  # some matches may be cut off; the rest is looked up one by one
            if field == fields[-1]:
                result.update({v.lower(): [] for v in remaining if v.lower() not in result})
            remaining = [v for v in remaining if v.lower() not in result]
        return result

    def find_many(self, kind, values, scope=None):
        values = [v.strip() for v in values if v and v.strip()]
        if kind == "identity":
            shape = lambda i: {"id": i["id"], "name": i.get("name"), "email": i.get("email")}  # noqa: E731
            path = "/v3/public-identities"
            ids = [v for v in values if ID_RE.match(v)]
            emails = [v for v in values if "@" in v and not ID_RE.match(v)]
            aliases = [v for v in values if v not in ids and v not in emails]
            out = {}
            for group, field in ((ids, "id"), (emails, "email"), (aliases, "alias")):
                if group:
                    out.update(self._bulk(kind, group, path, [field], shape=shape))
            return out
        if kind == "source":
            return self._bulk(kind, values, "/v3/sources", ["name"], shape=lambda s: {"id": s["id"], "name": s["name"]})
        if kind == "access_profile":
            return self._bulk(kind, values, "/v3/access-profiles", ["name"],
                              shape=lambda p: {"id": p["id"], "name": p["name"],
                                               "source": (p.get("source") or {}).get("name")})
        if kind == "governance_group":
            names = [v for v in values if not ID_RE.match(v) and not re.match(r"^[0-9a-f-]{36}$", v, re.I)]
            out = self._bulk(kind, names, f"/{self.wg_api}/workgroups", ["name"], headers=self.wg_headers,
                             shape=self._group)
            return out
        if kind == "entitlement":
            if not scope:
                return {}
            return self._bulk(kind, values, f"/{self.ent_api}/entitlements", ["name", "value"],
                              scope=f"source.id eq {q(scope)} and ",
                              shape=lambda e: {"id": e["id"], "name": e.get("name"), "value": e.get("value")})
        if kind.startswith("object:"):
            type_ = kind.split(":", 1)[1]
            path, headers = self._endpoint(type_)
            scoped = f"source.id eq {q(scope)} and " if scope and type_ == "entitlement" else ""
            shape = lambda o: model.normalize(type_, o)  # noqa: E731
            ids = [v for v in values if ID_RE.match(v)]
            names = [v for v in values if not ID_RE.match(v)]
            out = self._bulk(kind, ids, path, ["id"], scope=scoped, headers=headers, shape=shape) if ids else {}
            fields = ["name", "value"] if type_ == "entitlement" else ["name"]
            if names:
                out.update(self._bulk(kind, names, path, fields, scope=scoped, headers=headers, shape=shape))
            return out
        return super().find_many(kind, values, scope)

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
