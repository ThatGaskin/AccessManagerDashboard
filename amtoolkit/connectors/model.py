"""Shared object model for governed objects.

Connectors return API-shaped objects (as Identity Security Cloud does); `normalize` turns them into flat
dicts that actions compare and display, and `to_patch` turns a set of field changes back into JSON Patch
operations. The sandbox applies the same patches, so both connectors behave identically.
"""

TYPES = ("access_profile", "role", "entitlement", "source", "governance_group")
TYPE_LABELS = {"access_profile": "Access profile", "role": "Role", "entitlement": "Entitlement",
               "source": "Source", "governance_group": "Governance group"}
TYPE_ALIASES = {
    "access_profile": "access_profile", "accessprofile": "access_profile", "ap": "access_profile",
    "role": "role",
    "entitlement": "entitlement", "ent": "entitlement",
    "source": "source", "application": "source",
    "governance_group": "governance_group", "governancegroup": "governance_group", "workgroup": "governance_group",
    "group": "governance_group",
}
# Fields each type exposes for updates.
FIELDS = {
    "access_profile": ("description", "owner", "requestable", "enabled", "approval_schemes", "entitlements"),
    "role": ("description", "owner", "requestable", "enabled", "access_profiles"),
    "entitlement": ("description", "owner", "privileged", "requestable"),
    "source": ("owner",),
    "governance_group": ("description", "owner"),
}
APPROVER_TOKENS = {"MANAGER": "manager", "OWNER": "owner", "SOURCE_OWNER": "source_owner", "APP_OWNER": "app_owner"}
REQUESTABLE_TYPES = {"access_profile": "ACCESS_PROFILE", "role": "ROLE", "entitlement": "ENTITLEMENT"}


def parse_type(value):
    return TYPE_ALIASES.get((value or "").strip().lower().replace(" ", "_").replace("-", "_"))


def scheme_tokens(schemes):
    tokens = []
    for s in schemes or []:
        kind = s.get("approverType")
        if kind == "GOVERNANCE_GROUP":
            tokens.append(f"governance_group:{s.get('approverId')}")
        elif kind in APPROVER_TOKENS:
            tokens.append(APPROVER_TOKENS[kind])
    return tokens


def scheme_objects(tokens):
    reverse = {v: k for k, v in APPROVER_TOKENS.items()}
    out = []
    for t in tokens or []:
        if t.startswith("governance_group:"):
            out.append({"approverType": "GOVERNANCE_GROUP", "approverId": t.split(":", 1)[1]})
        else:
            out.append({"approverType": reverse[t], "approverId": None})
    return out


def _ids(refs):
    return sorted(r["id"] for r in refs or [] if r and r.get("id"))


def normalize(type_, obj):
    owner = obj.get("owner") or {}
    source = obj.get("source") or {}
    out = {"type": type_, "id": obj.get("id"), "name": obj.get("name"),
           "owner": owner.get("id"), "owner_name": owner.get("name") or owner.get("displayName")}
    if type_ == "access_profile":
        cfg = obj.get("accessRequestConfig") or {}
        out.update(description=obj.get("description") or "", requestable=bool(obj.get("requestable")),
                   enabled=bool(obj.get("enabled")), approval_schemes=scheme_tokens(cfg.get("approvalSchemes")),
                   entitlements=_ids(obj.get("entitlements")), source_id=source.get("id"),
                   source_name=source.get("name"))
    elif type_ == "role":
        out.update(description=obj.get("description") or "", requestable=bool(obj.get("requestable")),
                   enabled=bool(obj.get("enabled")), access_profiles=_ids(obj.get("accessProfiles")))
    elif type_ == "entitlement":
        out.update(description=obj.get("description") or "", privileged=bool(obj.get("privileged")),
                   requestable=bool(obj.get("requestable")), value=obj.get("value"),
                   source_id=source.get("id"), source_name=source.get("name"))
    elif type_ == "governance_group":
        count = obj.get("memberCount")
        if count is None and "members" in obj:
            count = len(obj["members"])
        out.update(description=obj.get("description") or "", member_count=count)
    return out


PATHS = {"description": "/description", "owner": "/owner", "requestable": "/requestable", "enabled": "/enabled",
         "privileged": "/privileged", "approval_schemes": "/accessRequestConfig/approvalSchemes",
         "entitlements": "/entitlements", "access_profiles": "/accessProfiles"}


def to_patch(changes):
    """JSON Patch (RFC 6902) operations for normalized field changes."""
    ops = []
    for field, value in changes.items():
        if field == "owner":
            value = {"type": "IDENTITY", "id": value}
        elif field == "approval_schemes":
            value = scheme_objects(value)
        elif field == "entitlements":
            value = [{"type": "ENTITLEMENT", "id": i} for i in value]
        elif field == "access_profiles":
            value = [{"type": "ACCESS_PROFILE", "id": i} for i in value]
        elif field == "description" and value == "":
            value = None
        # "add" replaces an existing member and also works when the parent object has no value yet.
        ops.append({"op": "add" if field == "approval_schemes" else "replace", "path": PATHS[field], "value": value})
    return ops


def apply_patch(obj, ops):
    """Minimal JSON Patch for the sandbox: add/replace on object paths."""
    for op in ops:
        parts = op["path"].strip("/").split("/")
        target = obj
        for part in parts[:-1]:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = op["value"]
    return obj


def same(a, b):
    if isinstance(a, list) or isinstance(b, list):
        return sorted(a or []) == sorted(b or [])
    if a in (None, "") and b in (None, ""):
        return True
    return a == b
