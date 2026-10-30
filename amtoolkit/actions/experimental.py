"""Experimental bulk actions.

Built from the Identity Security Cloud API documentation and tested against the offline sandbox only.
They use the same safety model as the stable actions: read-only planning, approval bound to the plan,
pause/resume, audit log, and rollback where an inverse exists. Updates additionally record the value of
every field before the change and refuse to apply when the object changed after planning.
"""
from ..connectors import model
from ..connectors.base import AuthError, ConfigError, ConnectorError
from ..util import parse_bool, split_list
from .access_profiles import (APPROVER_TYPES, NEW_GROUP_COLUMNS, NewGroups, _lookup_one, _resolve_group, _resolve_owner,
                              ensure_new_groups, groups_note)
from .base import Action, Column, ItemFailed, Outcome, Plan, msg

CLEAR = "(clear)"
# Approver token for a governance group the row creates; replaced by the real ID when the row runs.
NEW_GROUP_TOKEN = "governance_group:new:"


def _yes_no(value):
    return "yes" if value else "no"


def _fmt(value):
    if isinstance(value, bool):
        return _yes_no(value)
    if isinstance(value, list):
        return ", ".join(value) if value else "(none)"
    return f"'{value}'" if value not in (None, "") else "(empty)"


def _resolve_object(ctx, type_, ref, m, field_name, source_id=None):
    label = model.TYPE_LABELS[type_]
    return _lookup_one(ctx, ("object", type_, source_id), ref.lower(),
                       lambda: ctx.connector.find_objects(type_, ref, source_id), field_name, label, m, shown=ref)


def _resolve_identity(ctx, ref, field_name, label, m):
    return _lookup_one(ctx, "identity", ref, lambda: ctx.connector.find_identities(ref), field_name, label, m)


def _identity_name(ctx, identity_id):
    try:
        found = ctx.cached(("identity", identity_id), lambda: ctx.connector.find_identities(identity_id))
    except ConnectorError:
        found = []
    return found[0].get("name") or identity_id if found else identity_id


def _parse_bool_column(row, key, m):
    try:
        return parse_bool(row.get(key), None)
    except ValueError as exc:
        m.append(msg("error", str(exc), key, "invalid"))
        return None


def _parse_schemes(ctx, text, m, new_groups=None):
    """approval_schemes column → normalized tokens. 'none' means no approval."""
    if text.strip().lower() == "none":
        return []
    tokens = []
    for part in split_list(text):
        low = part.lower().replace(" ", "_")
        if low.startswith("governance_group:"):
            group = _resolve_group(ctx, part.split(":", 1)[1].strip(), m, new_groups=new_groups)
            if group and group.get("new"):
                tokens.append(NEW_GROUP_TOKEN + group["name"])
            elif group:
                tokens.append(f"governance_group:{group['id']}")
        elif low in APPROVER_TYPES:
            tokens.append(model.APPROVER_TOKENS[APPROVER_TYPES[low]])
        else:
            m.append(msg("error", f"Unknown approver '{part}'", "approval_schemes", "invalid"))
    return tokens


class ExperimentalAction(Action):
    experimental = True


class ObjectUpdateAction(ExperimentalAction):
    """Base for 'change some fields of existing objects'. Blank cells mean 'leave unchanged'."""

    def resolve(self, ctx, row, m):
        raise NotImplementedError

    def desired(self, ctx, row, current, m) -> dict:
        raise NotImplementedError

    def rollback_values(self, field, before, after) -> dict:
        raise NotImplementedError

    def identity_columns(self, obj) -> dict:
        raise NotImplementedError

    @property
    def inverse(self):
        return self.key

    def plan(self, ctx, row, options):
        m = []
        current = self.resolve(ctx, row, m)
        if current is None:
            return Plan("error", "", None, m)
        want = self.desired(ctx, row, current, m)
        if any(x["level"] == "error" for x in m):
            return Plan("error", current["name"], None, m)
        if not want:
            m.append(msg("error", "Nothing to change — fill in at least one of the change columns", None, "empty"))
            return Plan("error", current["name"], None, m)
        changes = {f: v for f, v in want.items() if not model.same(current.get(f), v)}
        if not changes:
            m.append(msg("info", "Already as requested; this row will be skipped", None, "unchanged"))
            return Plan("skip", current["name"], None, m)
        labels = {}
        for field, value in changes.items():
            before, after = current.get(field), value
            if field == "owner":
                before = current.get("owner_name") or before
                after = _identity_name(ctx, value)
            labels[field] = [_fmt(before), _fmt(after)]
            m.append(msg("info", f"{field}: {labels[field][0]} → {labels[field][1]}", field, "change"))
        payload = {"object": {"type": current["type"], "id": current["id"], "name": current["name"]},
                   "before": {f: current.get(f) for f in changes}, "after": changes, "labels": labels}
        new_groups = [ctx.cached(("new_governance_group", t[len(NEW_GROUP_TOKEN):].lower()), lambda: None)
                      for t in changes.get("approval_schemes") or [] if t.startswith(NEW_GROUP_TOKEN)]
        if new_groups:
            payload["new_governance_groups"] = new_groups
        return Plan("update", current["name"], payload, m)

    def execute(self, ctx, item, options):
        p = item["payload"]
        obj = p["object"]
        current = ctx.connector.get_object(obj["type"], obj["id"])
        if current is None:
            raise ItemFailed(f"{model.TYPE_LABELS[obj['type']]} no longer exists")
        drift = [f for f in p["after"] if not model.same(current.get(f), p["before"][f])]
        created_groups = []
        if p.get("new_governance_groups"):
            # When the object changed since planning, only look the groups up (an earlier attempt may have
            # applied this very change); they are created only when the change can still be applied.
            group_ids, created_groups = ensure_new_groups(ctx, p["new_governance_groups"],
                                                          (item.get("result") or {}).get("in_doubt"), create=not drift)
            p = dict(p, after=dict(p["after"], approval_schemes=[
                f"governance_group:{group_ids.get(t[len(NEW_GROUP_TOKEN):].lower(), t)}"
                if t.startswith(NEW_GROUP_TOKEN) else t for t in p["after"]["approval_schemes"]]))
        if all(model.same(current.get(f), v) for f, v in p["after"].items()):
            in_doubt = (item.get("result") or {}).get("in_doubt")
            return Outcome("succeeded" if in_doubt else "skipped", obj["id"], "Already has the requested values",
                           {"reconciled": bool(in_doubt)})
        if drift:
            raise ItemFailed(f"Changed by someone else since planning ({', '.join(drift)}). Re-check this row "
                             "to see the current values before applying.")
        try:
            ctx.connector.patch_object(obj["type"], obj["id"], p["after"])
        except ConnectorError as exc:
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            raise ItemFailed(str(exc), retryable=exc.retryable, in_doubt=exc.ambiguous) from None
        return Outcome("succeeded", obj["id"], "Updated " + ", ".join(p["after"]) + groups_note(created_groups),
                       {"before": p["before"], "created_groups": created_groups})

    def rollback_row(self, item):
        if item["status"] != "succeeded" or not item.get("payload"):
            return None
        p = item["payload"]
        row = self.identity_columns(p["object"])
        restorable = 0
        for field, before in p["before"].items():
            values = self.rollback_values(field, before, p["after"][field])
            if values is not None:  # e.g. "no owner" cannot be restored; the other fields still are
                row.update(values)
                restorable += 1
        return row if restorable else None


# ---------------------------------------------------------------------------- access profiles
class UpdateAccessProfiles(ObjectUpdateAction):
    key = "access_profile.update"
    title = "Update access profiles"
    description = ("Changes description, owner, requestable/enabled flags, approvers and entitlements of existing "
                   "access profiles. Blank cells leave a field unchanged; the plan shows every before → after.")
    columns = [
        Column("access_profile", True, "Access profile name or ID.", "Finance - Read"),
        Column("description", False, f"New description. {CLEAR} empties it.", "Read access to finance shares"),
        Column("owner", False, "New owner: a person (email, username or ID).", "carol@example.com"),
        Column("requestable", False, "yes/no", ""),
        Column("enabled", False, "yes/no", ""),
        Column("approval_schemes", False, "Replaces all approvers: manager;owner;governance_group:<name>… or none.",
               "manager;governance_group:Finance Approvers"),
        Column("add_entitlements", False, "Entitlements on the profile's source to add, separated by ';'.", ""),
        Column("remove_entitlements", False, "Entitlements to remove, separated by ';'.", ""),
        *[Column(c.name, False, c.help, "") for c in NEW_GROUP_COLUMNS],
    ]

    def duplicate_key(self, row):
        return (row.get("access_profile") or "").lower() or None

    def resolve(self, ctx, row, m):
        if not row.get("access_profile"):
            m.append(msg("error", "access_profile is required", "access_profile", "required"))
            return None
        return _resolve_object(ctx, "access_profile", row["access_profile"], m, "access_profile")

    def desired(self, ctx, row, current, m):
        want = {}
        if row.get("description"):
            want["description"] = "" if row["description"] == CLEAR else row["description"]
        if row.get("owner"):
            owner = _resolve_owner(ctx, row["owner"], m)
            if owner:
                want["owner"] = owner["id"]
        for key in ("requestable", "enabled"):
            value = _parse_bool_column(row, key, m)
            if value is not None:
                want[key] = value
        if row.get("approval_schemes"):
            want["approval_schemes"] = _parse_schemes(ctx, row["approval_schemes"], m, NewGroups(ctx, row, m))
        adds, removes = split_list(row.get("add_entitlements")), split_list(row.get("remove_entitlements"))
        if adds or removes:
            ents = set(current["entitlements"])
            for ref, target, label in [(r, True, "add_entitlements") for r in adds] + \
                                      [(r, False, "remove_entitlements") for r in removes]:
                ent = _resolve_object(ctx, "entitlement", ref, m, label, current["source_id"])
                if ent:
                    (ents.add if target else ents.discard)(ent["id"])
            if not ents:
                m.append(msg("error", "An access profile must keep at least one entitlement", "remove_entitlements",
                             "invalid"))
            want["entitlements"] = sorted(ents)
        return want

    def identity_columns(self, obj):
        return {"access_profile": obj["id"]}

    def rollback_values(self, field, before, after):
        if field == "description":
            return {"description": before or CLEAR}
        if field == "owner":
            return {"owner": before} if before else None
        if field in ("requestable", "enabled"):
            return {field: _yes_no(before)}
        if field == "approval_schemes":
            return {"approval_schemes": ";".join(before) or "none"}
        if field == "entitlements":
            return {"add_entitlements": ";".join(sorted(set(before) - set(after))),
                    "remove_entitlements": ";".join(sorted(set(after) - set(before)))}
        return None


# ---------------------------------------------------------------------------- ownership
class ReassignOwnership(ObjectUpdateAction):
    key = "ownership.reassign"
    title = "Reassign ownership"
    description = ("Moves ownership of access profiles, roles, entitlements, sources and governance groups to "
                   "another person — e.g. when an owner leaves. Use the 'Objects owned by an identity' report to "
                   "build the CSV.")
    columns = [
        Column("object_type", True, "access_profile, role, entitlement, source or governance_group.", "access_profile"),
        Column("object", True, "Name or ID of the object.", "Finance - Read"),
        Column("source", False, "Source name — only needed for entitlements given by name.", ""),
        Column("new_owner", True, "New owner: a person (email, username or ID).", "carol@example.com"),
        Column("current_owner", False, "Optional safety check: the row fails if the object is not currently owned "
               "by this person.", "alice@example.com"),
    ]

    def duplicate_key(self, row):
        return f"{model.parse_type(row.get('object_type'))}:{(row.get('object') or '').lower()}" \
            if row.get("object") else None

    def resolve(self, ctx, row, m):
        type_ = model.parse_type(row.get("object_type"))
        if type_ is None:
            m.append(msg("error", "object_type must be one of: " + ", ".join(model.TYPES), "object_type", "invalid"))
            return None
        if not row.get("object"):
            m.append(msg("error", "object is required", "object", "required"))
            return None
        source_id = None
        if type_ == "entitlement" and row.get("source"):
            source = _lookup_one(ctx, "source", row["source"].lower(), lambda: ctx.connector.find_sources(row["source"]),
                                 "source", "Source", m, shown=row["source"])
            if source is None:
                return None
            source_id = source["id"]
        return _resolve_object(ctx, type_, row["object"], m, "object", source_id)

    def desired(self, ctx, row, current, m):
        if row.get("current_owner"):
            expected = _resolve_identity(ctx, row["current_owner"], "current_owner", "Current owner", m)
            if expected and expected["id"] != current["owner"]:
                m.append(msg("error", f"Currently owned by {current.get('owner_name') or current['owner'] or 'nobody'}, "
                                      f"not {expected.get('name')}", "current_owner", "owner_mismatch"))
        if not row.get("new_owner"):
            m.append(msg("error", "new_owner is required", "new_owner", "required"))
            return {}
        owner = _resolve_owner(ctx, row["new_owner"], m)
        return {"owner": owner["id"]} if owner else {}

    def identity_columns(self, obj):
        return {"object_type": obj["type"], "object": obj["id"]}

    def rollback_values(self, field, before, after):
        return {"new_owner": before, "current_owner": after} if before else None


# ---------------------------------------------------------------------------- entitlements
class UpdateEntitlements(ObjectUpdateAction):
    key = "entitlement.update"
    title = "Update entitlements"
    description = ("Fills in or corrects entitlement metadata: description, owner, privileged and requestable "
                   "flags. Use the 'Entitlements missing description or owner' report to build the CSV.")
    columns = [
        Column("source", True, "Source name.", "Active Directory"),
        Column("entitlement", True, "Entitlement name, value or ID.", "Finance-Write"),
        Column("description", False, f"New description. {CLEAR} empties it.", "Write access to finance shares"),
        Column("owner", False, "New owner: a person (email, username or ID).", "alice@example.com"),
        Column("privileged", False, "yes/no", ""),
        Column("requestable", False, "yes/no", ""),
    ]

    def duplicate_key(self, row):
        return f"{(row.get('source') or '').lower()}:{(row.get('entitlement') or '').lower()}" \
            if row.get("entitlement") else None

    def resolve(self, ctx, row, m):
        if not row.get("source") or not row.get("entitlement"):
            m.append(msg("error", "source and entitlement are required", "entitlement", "required"))
            return None
        source = _lookup_one(ctx, "source", row["source"].lower(), lambda: ctx.connector.find_sources(row["source"]),
                             "source", "Source", m, shown=row["source"])
        if source is None:
            return None
        return _resolve_object(ctx, "entitlement", row["entitlement"], m, "entitlement", source["id"])

    def desired(self, ctx, row, current, m):
        want = {}
        if row.get("description"):
            want["description"] = "" if row["description"] == CLEAR else row["description"]
        if row.get("owner"):
            owner = _resolve_owner(ctx, row["owner"], m)
            if owner:
                want["owner"] = owner["id"]
        for key in ("privileged", "requestable"):
            value = _parse_bool_column(row, key, m)
            if value is not None:
                want[key] = value
        return want

    def identity_columns(self, obj):
        return {"entitlement": obj["id"]}

    def rollback_row(self, item):
        row = super().rollback_row(item)
        if row is not None:
            row["source"] = (item["input"] or {}).get("source", "")
        return row

    def rollback_values(self, field, before, after):
        if field == "description":
            return {"description": before or CLEAR}
        if field == "owner":
            return {"owner": before} if before else None
        return {field: _yes_no(before)}


# ---------------------------------------------------------------------------- governance group members
class ChangeGroupMembers(ExperimentalAction):
    key = "governance_group.members"
    title = "Change governance group members"
    description = ("Adds and removes members of existing governance groups — e.g. to replace someone who left "
                   "or to sync a team list. Only real changes are planned.")
    inverse = "governance_group.members"
    columns = [
        Column("group", True, "Governance group name or ID.", "Finance Approvers"),
        Column("add", False, "Identities to add (email, username or ID), separated by ';'.", "bob@example.com"),
        Column("remove", False, "Identities to remove, separated by ';'.", "carol"),
    ]

    def duplicate_key(self, row):
        return (row.get("group") or "").lower() or None

    def plan(self, ctx, row, options):
        m = []
        c = ctx.connector
        if not row.get("group"):
            return Plan("error", "", None, [msg("error", "group is required", "group", "required")])
        group = _lookup_one(ctx, "governance_group", row["group"].lower(),
                            lambda: c.find_governance_groups(row["group"]), "group", "Governance group", m,
                            shown=row["group"])
        if group is None:
            return Plan("error", row["group"], None, m)
        try:
            members = {x["id"] for x in c.list_governance_group_members(group["id"])}
        except (AuthError, ConfigError):
            raise
        except ConnectorError as exc:
            return Plan("error", group["name"], None, m + [msg("error", f"Could not read members: {exc}", "group",
                                                               "lookup_failed")])
        add, remove = [], []
        for ref in split_list(row.get("add")):
            ident = _resolve_identity(ctx, ref, "add", "Identity", m)
            if ident and ident["id"] in members:
                m.append(msg("info", f"{ident.get('name') or ref} is already a member", "add"))
            elif ident:
                add.append({"id": ident["id"], "name": ident.get("name")})
        for ref in split_list(row.get("remove")):
            ident = _resolve_identity(ctx, ref, "remove", "Identity", m)
            if ident and ident["id"] not in members:
                m.append(msg("info", f"{ident.get('name') or ref} is not a member", "remove"))
            elif ident:
                remove.append({"id": ident["id"], "name": ident.get("name")})
        if any(x["level"] == "error" for x in m):
            return Plan("error", group["name"], None, m)
        if not add and not remove:
            m.append(msg("info", "No membership changes needed; this row will be skipped", None, "unchanged"))
            return Plan("skip", group["name"], None, m)
        if len(members) + len(add) - len(remove) == 0:
            m.append(msg("warning", "The group will have no members; approvals assigned to it cannot be completed",
                         "remove"))
        return Plan("update", group["name"], {"group": {"id": group["id"], "name": group["name"]},
                                              "add": add, "remove": remove}, m)

    def execute(self, ctx, item, options):
        c = ctx.connector
        p = item["payload"]
        gid = p["group"]["id"]
        try:
            members = {x["id"] for x in c.list_governance_group_members(gid)}
            add = [x["id"] for x in p["add"] if x["id"] not in members]
            remove = [x["id"] for x in p["remove"] if x["id"] in members]
            problems = c.add_governance_group_members(gid, add) + c.remove_governance_group_members(gid, remove)
        except ConnectorError as exc:
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            raise ItemFailed(str(exc), retryable=exc.retryable, in_doubt=True) from None
        if problems:
            raise ItemFailed("; ".join(problems), in_doubt=True)
        if not add and not remove:
            return Outcome("skipped", gid, "Membership already as planned")
        return Outcome("succeeded", gid, f"Added {len(add)}, removed {len(remove)} member(s)",
                       {"added": add, "removed": remove})

    def rollback_row(self, item):
        if item["status"] != "succeeded":
            return None
        r = item.get("result") or {}
        return {"group": item["payload"]["group"]["id"], "add": ";".join(r.get("removed", [])),
                "remove": ";".join(r.get("added", []))}


# ---------------------------------------------------------------------------- roles
class CreateRoles(ExperimentalAction):
    key = "role.create"
    title = "Create roles"
    description = "Creates roles that bundle existing access profiles."
    inverse = "role.delete"
    columns = [
        Column("name", True, "Unique role name.", "Finance Analyst"),
        Column("description", False, "", "Everything a finance analyst needs"),
        Column("owner", True, "Owner: a person (email, username or ID).", "alice@example.com"),
        Column("access_profiles", True, "Access profile names or IDs, separated by ';'.", "Finance - Read;Remote Access"),
        Column("requestable", False, "yes/no — default no.", "no"),
        Column("enabled", False, "yes/no — default yes.", "yes"),
    ]

    def duplicate_key(self, row):
        return (row.get("name") or "").lower() or None

    def plan(self, ctx, row, options):
        m = []
        name = row.get("name", "")
        if not name:
            m.append(msg("error", "Name is required", "name", "required"))
        owner = None
        if row.get("owner"):
            owner = _resolve_owner(ctx, row["owner"], m)
        else:
            m.append(msg("error", "Owner is required", "owner", "required"))
        aps = []
        refs = split_list(row.get("access_profiles"))
        if not refs:
            m.append(msg("error", "At least one access profile is required", "access_profiles", "required"))
        for ref in refs:
            ap = _resolve_object(ctx, "access_profile", ref, m, "access_profiles")
            if ap:
                aps.append({"type": "ACCESS_PROFILE", "id": ap["id"], "name": ap["name"]})
        flags = {}
        for key, default in (("requestable", False), ("enabled", True)):
            try:
                flags[key] = parse_bool(row.get(key), default)
            except ValueError as exc:
                m.append(msg("error", str(exc), key, "invalid"))
        if name:
            existing = ctx.connector.find_objects("role", name)
            if existing:
                m.append(msg("info", f"Already exists ({existing[0]['id']}); this row will be skipped", "name", "exists"))
                return Plan("skip", name, None, m)
        if any(x["level"] == "error" for x in m):
            return Plan("error", name, None, m)
        return Plan("create", name, {"name": name, "description": row.get("description") or None,
                                     "owner": {"type": "IDENTITY", "id": owner["id"], "name": owner.get("name")},
                                     "accessProfiles": aps, **flags}, m)

    def execute(self, ctx, item, options):
        c = ctx.connector
        payload = item["payload"]
        existing = c.find_objects("role", payload["name"])
        in_doubt = (item.get("result") or {}).get("in_doubt")
        if existing:
            if in_doubt:
                return Outcome("succeeded", existing[0]["id"], "Found the role from an earlier interrupted attempt",
                               {"reconciled": True})
            return Outcome("skipped", existing[0]["id"], "Already exists; created by someone else after planning")
        try:
            created = c.create_role(payload)
        except ConnectorError as exc:
            again = c.find_objects("role", payload["name"]) if exc.ambiguous else []
            if again:
                return Outcome("succeeded", again[0]["id"], "Created (confirmed after an unclear response)",
                               {"reconciled": True})
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            raise ItemFailed(str(exc), retryable=exc.retryable, in_doubt=exc.ambiguous) from None
        return Outcome("succeeded", created.get("id"), "Created", {"id": created.get("id")})

    def rollback_row(self, item):
        if item["status"] == "succeeded" and item.get("remote_id"):
            return {"id": item["remote_id"], "name": item["payload"]["name"]}
        return None


class DeleteRoles(ExperimentalAction):
    key = "role.delete"
    title = "Delete roles"
    description = "Deletes roles by ID (preferred) or exact name. Used for rollback of 'Create roles'."
    columns = [Column("id", False, "Role ID.", ""), Column("name", False, "Exact role name.", "Finance Analyst")]

    def duplicate_key(self, row):
        return (row.get("id") or row.get("name") or "").lower() or None

    def plan(self, ctx, row, options):
        ref = row.get("id") or row.get("name")
        if not ref:
            return Plan("error", "", None, [msg("error", "Either id or name is required", "id", "required")])
        found = ctx.connector.find_objects("role", ref)
        if len(found) > 1:
            return Plan("error", ref, None, [msg("error", f"'{ref}' matches {len(found)} roles; use the ID", "name",
                                                 "ambiguous")])
        if not found:
            return Plan("skip", ref, None, [msg("info", "Not found (already deleted?); skipped", "id", "missing")])
        return Plan("delete", found[0]["name"], {"id": found[0]["id"], "name": found[0]["name"]}, [])

    def execute(self, ctx, item, options):
        try:
            ctx.connector.delete_role(item["payload"]["id"])
        except ConnectorError as exc:
            if exc.status == 404:
                return Outcome("skipped", item["payload"]["id"], "Already gone")
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            raise ItemFailed(str(exc), retryable=exc.retryable) from None
        return Outcome("succeeded", item["payload"]["id"], "Deleted")


# ---------------------------------------------------------------------------- access requests
REQUEST_TYPES = {"grant": "GRANT_ACCESS", "grant_access": "GRANT_ACCESS", "revoke": "REVOKE_ACCESS",
                 "revoke_access": "REVOKE_ACCESS", "": "GRANT_ACCESS"}


class SubmitAccessRequests(ExperimentalAction):
    key = "access_request.submit"
    title = "Submit access requests"
    description = ("Requests or revokes access for many people at once — e.g. onboarding a project team or a "
                   "reorganisation. Requests go through the normal approval process in the tenant. Rollback "
                   "submits the opposite request.")
    inverse = "access_request.submit"
    columns = [
        Column("requested_for", True, "Identity (email, username or ID).", "bob@example.com"),
        Column("access_type", True, "access_profile, role or entitlement.", "access_profile"),
        Column("access", True, "Name or ID of the access profile / role / entitlement.", "Finance - Read"),
        Column("source", False, "Source name — only for entitlements given by name.", ""),
        Column("request_type", False, "grant (default) or revoke.", "grant"),
        Column("comment", True, "Business justification, shown to approvers.", "Joining the Q4 finance project"),
    ]

    def duplicate_key(self, row):
        return f"{(row.get('requested_for') or '').lower()}|{(row.get('access') or '').lower()}" \
            if row.get("access") else None

    def plan(self, ctx, row, options):
        m = []
        rtype = REQUEST_TYPES.get((row.get("request_type") or "").strip().lower())
        if rtype is None:
            m.append(msg("error", "request_type must be grant or revoke", "request_type", "invalid"))
        type_ = model.parse_type(row.get("access_type"))
        if type_ not in model.REQUESTABLE_TYPES:
            m.append(msg("error", "access_type must be access_profile, role or entitlement", "access_type", "invalid"))
        if not row.get("comment"):
            m.append(msg("error", "A comment is required; approvers see it", "comment", "required"))
        who = None
        if row.get("requested_for"):
            who = _resolve_identity(ctx, row["requested_for"], "requested_for", "Identity", m)
        else:
            m.append(msg("error", "requested_for is required", "requested_for", "required"))
        item = None
        if type_ in model.REQUESTABLE_TYPES and row.get("access"):
            source_id = None
            if type_ == "entitlement" and row.get("source"):
                src = _lookup_one(ctx, "source", row["source"].lower(),
                                  lambda: ctx.connector.find_sources(row["source"]), "source", "Source", m,
                                  shown=row["source"])
                source_id = src["id"] if src else None
            item = _resolve_object(ctx, type_, row["access"], m, "access", source_id)
            if item and type_ != "role" and item.get("requestable") is False and rtype == "GRANT_ACCESS":
                m.append(msg("warning", f"'{item['name']}' is not marked requestable; the tenant may reject the "
                                        "request", "access"))
        if any(x["level"] == "error" for x in m) or not who or not item:
            return Plan("error", row.get("access", ""), None, m)
        pending = [r for r in ctx.connector.find_access_requests(who["id"]) if r.get("item_id") == item["id"]]
        if pending:
            m.append(msg("info", f"A request for this is already pending ({pending[0]['id']}); skipped", None,
                         "pending"))
            return Plan("skip", f"{who.get('name')} ← {item['name']}", None, m)
        verb = "Grant" if rtype == "GRANT_ACCESS" else "Revoke"
        m.append(msg("info", f"{verb} {model.TYPE_LABELS[type_].lower()} '{item['name']}' for {who.get('name')}"))
        return Plan("request", f"{who.get('name')} ← {item['name']}", {
            "requestedFor": [who["id"]], "requestType": rtype,
            "requestedItems": [{"type": model.REQUESTABLE_TYPES[type_], "id": item["id"], "name": item["name"],
                                "comment": row["comment"]}],
            "display": {"identity": who.get("name"), "access_type": type_}}, m)

    def execute(self, ctx, item, options):
        c = ctx.connector
        p = item["payload"]
        who, target = p["requestedFor"][0], p["requestedItems"][0]["id"]
        if (item.get("result") or {}).get("in_doubt"):
            if any(r.get("item_id") == target for r in c.find_access_requests(who)):
                return Outcome("succeeded", target, "Found the request from an earlier interrupted attempt",
                               {"reconciled": True})
        body = {k: v for k, v in p.items() if k != "display"}
        body["requestedItems"] = [{k: v for k, v in i.items() if k != "name"} for i in p["requestedItems"]]
        try:
            result = c.submit_access_request(body)
        except ConnectorError as exc:
            if exc.ambiguous and any(r.get("item_id") == target for r in c.find_access_requests(who)):
                return Outcome("succeeded", target, "Submitted (confirmed after an unclear response)",
                               {"reconciled": True})
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            raise ItemFailed(str(exc), retryable=exc.retryable, in_doubt=exc.ambiguous) from None
        return Outcome("succeeded", target, "Request submitted; it now follows the tenant's approval process",
                       {"response": result})

    def rollback_row(self, item):
        if item["status"] != "succeeded":
            return None
        p = item["payload"]
        req = p["requestedItems"][0]
        return {"requested_for": p["requestedFor"][0], "access_type": p["display"]["access_type"],
                "access": req["id"], "request_type": "revoke" if p["requestType"] == "GRANT_ACCESS" else "grant",
                "comment": f"Rollback of bulk request: {req['comment']}"}


class CancelAccessRequests(ExperimentalAction):
    key = "access_request.cancel"
    title = "Cancel access requests"
    description = "Cancels pending access requests, e.g. stale ones or ones submitted by mistake."
    columns = [
        Column("request_id", True, "Access request ID.", ""),
        Column("comment", True, "Reason for cancelling.", "Submitted by mistake"),
    ]

    def duplicate_key(self, row):
        return (row.get("request_id") or "").lower() or None

    def plan(self, ctx, row, options):
        m = []
        if not row.get("comment"):
            m.append(msg("error", "A comment is required", "comment", "required"))
        if not row.get("request_id"):
            return Plan("error", "", None, m + [msg("error", "request_id is required", "request_id", "required")])
        req = ctx.connector.get_access_request(row["request_id"])
        if req is None:
            return Plan("error", row["request_id"], None, m + [msg("error", "Access request not found", "request_id",
                                                                   "not_found")])
        label = f"{req.get('item_name') or req['id']}"
        if req.get("state") not in ("PENDING", "EXECUTING"):
            return Plan("skip", label, None, m + [msg("info", f"Already {str(req.get('state')).lower()}; skipped",
                                                      "request_id")])
        if any(x["level"] == "error" for x in m):
            return Plan("error", label, None, m)
        return Plan("cancel", label, {"id": req["id"], "comment": row["comment"], "item": req.get("item_name")}, m)

    def execute(self, ctx, item, options):
        p = item["payload"]
        try:
            ctx.connector.cancel_access_request(p["id"], p["comment"])
        except ConnectorError as exc:
            current = ctx.connector.get_access_request(p["id"])
            if current and current.get("state") not in ("PENDING", "EXECUTING"):
                return Outcome("skipped", p["id"], f"Already {str(current.get('state')).lower()}")
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            raise ItemFailed(str(exc), retryable=exc.retryable) from None
        return Outcome("succeeded", p["id"], "Cancelled")


class ForwardApprovals(ExperimentalAction):
    key = "approval.forward"
    title = "Forward pending approvals"
    description = ("Reassigns pending access request approvals to another approver — e.g. when the approver is on "
                   "leave or has left. Rollback forwards them back.")
    inverse = "approval.forward"
    columns = [
        Column("approval_id", True, "Pending approval ID.", ""),
        Column("new_approver", True, "Person to forward to (email, username or ID).", "alice@example.com"),
        Column("comment", True, "Reason, visible in the approval history.", "Bob is on leave"),
    ]

    def duplicate_key(self, row):
        return (row.get("approval_id") or "").lower() or None

    def plan(self, ctx, row, options):
        m = []
        if not row.get("comment"):
            m.append(msg("error", "A comment is required", "comment", "required"))
        if not row.get("approval_id"):
            return Plan("error", "", None, m + [msg("error", "approval_id is required", "approval_id", "required")])
        approval = ctx.connector.get_pending_approval(row["approval_id"])
        if approval is None:
            return Plan("error", row["approval_id"], None,
                        m + [msg("error", "Approval not found or no longer pending", "approval_id", "not_found")])
        label = f"{approval.get('item_name')} for {approval.get('requested_for')}"
        new = None
        if row.get("new_approver"):
            new = _resolve_owner(ctx, row["new_approver"], m)
        else:
            m.append(msg("error", "new_approver is required", "new_approver", "required"))
        if any(x["level"] == "error" for x in m):
            return Plan("error", label, None, m)
        if new["id"] == approval["approver_id"]:
            return Plan("skip", label, None, m + [msg("info", "Already assigned to this approver; skipped",
                                                      "new_approver")])
        m.append(msg("info", f"approver: {approval.get('approver_name')} → {new.get('name')}", "new_approver"))
        return Plan("forward", label, {"id": approval["id"], "new_owner_id": new["id"], "new_owner": new.get("name"),
                                       "before_owner_id": approval["approver_id"], "comment": row["comment"]}, m)

    def execute(self, ctx, item, options):
        p = item["payload"]
        current = ctx.connector.get_pending_approval(p["id"])
        if current is None:
            raise ItemFailed("No longer pending (approved, rejected or cancelled since planning)")
        if current["approver_id"] == p["new_owner_id"]:
            return Outcome("skipped", p["id"], "Already assigned to the new approver")
        if current["approver_id"] != p["before_owner_id"]:
            raise ItemFailed("Reassigned by someone else since planning; re-check this row")
        try:
            ctx.connector.forward_approval(p["id"], p["new_owner_id"], p["comment"])
        except ConnectorError as exc:
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            raise ItemFailed(str(exc), retryable=exc.retryable, in_doubt=exc.ambiguous) from None
        return Outcome("succeeded", p["id"], f"Forwarded to {p['new_owner']}")

    def rollback_row(self, item):
        if item["status"] != "succeeded":
            return None
        p = item["payload"]
        return {"approval_id": p["id"], "new_approver": p["before_owner_id"], "comment": f"Rollback: {p['comment']}"}
