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


def _names(refs):
    return {r["id"]: r.get("name") or r["id"] for r in refs or [] if r and r.get("id")}


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
                   source_name=source.get("name"), comments_required=bool(cfg.get("commentsRequired")),
                   denial_comments_required=bool(cfg.get("denialCommentsRequired")),
                   entitlement_names=_names(obj.get("entitlements")))
    elif type_ == "role":
        out.update(description=obj.get("description") or "", requestable=bool(obj.get("requestable")),
                   enabled=bool(obj.get("enabled")), access_profiles=_ids(obj.get("accessProfiles")),
                   access_profile_names=_names(obj.get("accessProfiles")))
    elif type_ == "source":
        cluster = obj.get("cluster") or {}
        group = obj.get("managementWorkgroup") or {}
        out.update(description=obj.get("description") or "", connector=obj.get("connector"),
                   cluster=cluster.get("id"), cluster_name=cluster.get("name"),
                   delete_threshold=obj.get("deleteThreshold"), authoritative=bool(obj.get("authoritative")),
                   management_workgroup=group.get("id"), management_workgroup_name=group.get("name"),
                   config=dict(obj.get("connectorAttributes") or {}))
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
         "entitlements": "/entitlements", "access_profiles": "/accessProfiles",
         "comments_required": "/accessRequestConfig/commentsRequired",
         "denial_comments_required": "/accessRequestConfig/denialCommentsRequired",
         "delete_threshold": "/deleteThreshold", "authoritative": "/authoritative", "cluster": "/cluster",
         "management_workgroup": "/managementWorkgroup"}
# Fields inside a parent object that may not exist yet: "add" creates them, "replace" would fail.
ADD_PATHS = ("approval_schemes", "comments_required", "denial_comments_required")
CONFIG_PREFIX = "config."  # a source's connector attribute, e.g. "config.host"


def _pointer(key):
    return key.replace("~", "~0").replace("/", "~1")


def to_patch(changes):
    """JSON Patch (RFC 6902) operations for normalized field changes."""
    ops = []
    for field, value in changes.items():
        if field.startswith(CONFIG_PREFIX):
            path = "/connectorAttributes/" + _pointer(field[len(CONFIG_PREFIX):])
            ops.append({"op": "remove", "path": path} if value is None else {"op": "add", "path": path, "value": value})
            continue
        if field in ("cluster", "management_workgroup") and not value:
            ops.append({"op": "remove", "path": PATHS[field]})
            continue
        if field == "cluster":
            value = {"type": "CLUSTER", "id": value}
        elif field == "management_workgroup":
            value = {"type": "GOVERNANCE_GROUP", "id": value}
        elif field == "owner":
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
        ops.append({"op": "add" if field in ADD_PATHS else "replace", "path": PATHS[field], "value": value})
    return ops


def apply_patch(obj, ops):
    """Minimal JSON Patch for the sandbox: add/replace/remove on object paths."""
    for op in ops:
        parts = [p.replace("~1", "/").replace("~0", "~") for p in op["path"].strip("/").split("/")]
        target = obj
        for part in parts[:-1]:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]
        if op["op"] == "remove":
            target.pop(parts[-1], None)
        else:
            target[parts[-1]] = op["value"]
    return obj


def same(a, b):
    if isinstance(a, list) or isinstance(b, list):
        return sorted(a or []) == sorted(b or [])
    if a in (None, "") and b in (None, ""):
        return True
    return a == b
