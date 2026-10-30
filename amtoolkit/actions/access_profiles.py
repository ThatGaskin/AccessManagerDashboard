"""Bulk create and bulk delete of access profiles."""
import copy
import re

from ..connectors.base import AuthError, ConfigError, ConnectorError
from ..util import parse_bool, split_list
from .base import Action, Column, ItemFailed, Option, Outcome, Plan, msg

APPROVER_TYPES = {
    "manager": "MANAGER",
    "owner": "OWNER",
    "source_owner": "SOURCE_OWNER",
    "sourceowner": "SOURCE_OWNER",
    "app_owner": "APP_OWNER",
    "appowner": "APP_OWNER",
}
MAX_NAME = 128
MAX_DESCRIPTION = 2000
MAX_GROUP_NAME = 128

# Optional columns of every action whose approval_schemes can name governance groups. Filling in
# new_group_owner lets the row create the groups it names that do not exist yet.
NEW_GROUP_COLUMNS = [
    Column("new_group_owner", False, "Fill in to create governance groups named in approval_schemes that do not "
           "exist yet: the owner of the new groups (email, username or identity ID). Blank = a missing group is "
           "flagged as an error.", "alice@example.com"),
    Column("new_group_members", False, "Members of the new groups (email, alias or ID), separated by ';'. A group "
           "without members cannot approve anything.", "alice@example.com;carol"),
    Column("new_group_description", False, "Description of the new groups.", "Approves access to finance systems"),
]


def _lookup_one(ctx, kind, key, fn, field_name, label, messages, shown=None):
    """Resolves a reference; adds an error message for zero or multiple matches."""
    try:
        found = ctx.cached((kind, key), fn)
    except (AuthError, ConfigError):
        raise
    except ConnectorError as exc:
        messages.append(msg("error", f"Could not look up {label} '{shown or key}': {exc}", field_name,
                            "lookup_failed"))
        return None
    shown = key if shown is None else shown
    if not found:
        hint = " (the identity may have been deleted)" if kind == "identity" and re.fullmatch(r"[0-9a-f]{32}", str(shown)) \
            else ""
        messages.append(msg("error", f"{label} '{shown}' was not found{hint}", field_name, "not_found"))
        return None
    if len(found) > 1:
        messages.append(msg("error", f"{label} '{shown}' matches {len(found)} records; use the ID instead",
                            field_name, "ambiguous"))
        return None
    return found[0]


class CreateAccessProfiles(Action):
    key = "access_profile.create"
    title = "Create access profiles"
    description = ("Creates one access profile per row. Owner, source and entitlements are looked up by "
                   "name so the CSV stays human-readable; the plan shows the exact IDs that will be sent.")
    inverse = "access_profile.delete"
    columns = [
        Column("name", True, f"Unique access profile name (max {MAX_NAME} characters).", "Finance - Read"),
        Column("description", False, "Shown to requesters in the access request UI.",
               "Read access to finance shares"),
        Column("owner", True, "Owner identity: email, username (alias) or identity ID. Must be a person — "
               "governance groups cannot own access profiles (use them as approvers).", "alice@example.com"),
        Column("source", True, "Source name exactly as it appears in the tenant.", "Active Directory"),
        Column("entitlements", True, "Entitlement names or values on that source, separated by ';'.",
               "Finance-Read;VPN Users"),
        Column("requestable", False, "yes/no — can users request it? Default yes.", "yes"),
        Column("enabled", False, "yes/no — default yes.", "yes"),
        Column("approval_schemes", False,
               "Approvers in order, separated by ';': manager, owner, source_owner, app_owner, "
               "governance_group:<group name or ID>. Blank = no approval.", "manager;governance_group:Finance Approvers"),
        Column("comments_required", False, "yes/no — requester must comment. Default no.", "no"),
        Column("denial_comments_required", False, "yes/no — approver must comment on denial. Default no.", "no"),
        *NEW_GROUP_COLUMNS,
    ]
    options = [
        Option("if_exists", "If a profile with the same name already exists", "skip", ["skip", "error"],
               "skip = leave the existing profile alone; error = flag the row so you must decide."),
    ]

    def duplicate_key(self, row):
        return (row.get("name") or "").strip().lower() or None

    def plan(self, ctx, row, options):
        m = []
        c = ctx.connector
        name = row.get("name", "")
        if not name:
            m.append(msg("error", "Name is required", "name", "required"))
        elif len(name) > MAX_NAME:
            m.append(msg("error", f"Name is longer than {MAX_NAME} characters", "name", "too_long"))
        description = row.get("description", "")
        if len(description) > MAX_DESCRIPTION:
            m.append(msg("error", f"Description is longer than {MAX_DESCRIPTION} characters", "description",
                         "too_long"))
        elif not description:
            m.append(msg("warning", "No description; requesters will not know what this grants", "description"))

        flags = {}
        for key, default in (("requestable", True), ("enabled", True), ("comments_required", False),
                             ("denial_comments_required", False)):
            try:
                flags[key] = parse_bool(row.get(key), default)
            except ValueError as exc:
                m.append(msg("error", str(exc), key, "invalid"))

        schemes = []
        new_groups = NewGroups(ctx, row, m)
        for part in split_list(row.get("approval_schemes")):
            low = part.lower().replace(" ", "_")
            if low.startswith("governance_group:"):
                group = _resolve_group(ctx, part.split(":", 1)[1].strip(), m, new_groups=new_groups)
                if group and group.get("new"):
                    # The ID is only known once the group is created, when this row runs.
                    schemes.append({"approverType": "GOVERNANCE_GROUP", "approverId": None, "newGroup": group["name"]})
                elif group:
                    schemes.append({"approverType": "GOVERNANCE_GROUP", "approverId": group["id"]})
                continue
            if low in APPROVER_TYPES:
                schemes.append({"approverType": APPROVER_TYPES[low], "approverId": None})
            else:
                m.append(msg("error", f"Unknown approver '{part}'. Use manager, owner, source_owner, app_owner or "
                                      "governance_group:<name>", "approval_schemes", "invalid"))
        if flags.get("requestable") and not schemes:
            m.append(msg("warning", "Requestable without approval: requests are granted automatically",
                         "approval_schemes"))

        owner = source = None
        if not row.get("owner"):
            m.append(msg("error", "Owner is required", "owner", "required"))
        else:
            owner = _resolve_owner(ctx, row["owner"], m)
        if not row.get("source"):
            m.append(msg("error", "Source is required", "source", "required"))
        else:
            source = _lookup_one(ctx, "source", row["source"].lower(), lambda: c.find_sources(row["source"]),
                                 "source", "Source", m, shown=row["source"])

        entitlements = []
        wanted = split_list(row.get("entitlements"))
        if not wanted:
            m.append(msg("error", "At least one entitlement is required", "entitlements", "required"))
        elif source:
            seen = set()
            for value in wanted:
                ent = _lookup_one(ctx, "entitlement", (source["id"], value.lower()),
                                  lambda v=value: c.find_entitlements(source["id"], v), "entitlements",
                                  "Entitlement", m, shown=value)
                if ent and ent["id"] not in seen:
                    seen.add(ent["id"])
                    entitlements.append({"type": "ENTITLEMENT", "id": ent["id"], "name": ent["name"]})
                elif ent:
                    m.append(msg("warning", f"Entitlement '{value}' is listed twice", "entitlements"))

        if name and len(name) <= MAX_NAME:
            existing = _lookup_one_or_none(ctx, name, m)
            if existing:
                if options.get("if_exists") == "error":
                    m.append(msg("error", f"An access profile named '{name}' already exists ({existing['id']})",
                                 "name", "exists"))
                else:
                    m.append(msg("info", f"Already exists ({existing['id']}); this row will be skipped", "name",
                                 "exists"))
                    return Plan("skip", name, {"existing_id": existing["id"]}, m)

        if any(x["level"] == "error" for x in m):
            return Plan("error", name, None, m)

        payload = {
            "name": name,
            "description": description or None,
            "owner": {"type": "IDENTITY", "id": owner["id"], "name": owner.get("name")},
            "source": {"type": "SOURCE", "id": source["id"], "name": source["name"]},
            "entitlements": entitlements,
            "enabled": flags["enabled"],
            "requestable": flags["requestable"],
            "accessRequestConfig": {
                "commentsRequired": flags["comments_required"],
                "denialCommentsRequired": flags["denial_comments_required"],
                "approvalSchemes": schemes,
            },
        }
        if new_groups.specs():
            payload["new_governance_groups"] = new_groups.specs()
        return Plan("create", name, payload, m)

    def execute(self, ctx, item, options):
        c = ctx.connector
        payload = copy.deepcopy(item["payload"])
        specs = payload.pop("new_governance_groups", None)
        name = payload["name"]
        # Idempotency: check first. Catches profiles created by someone else since planning, and
        # profiles we created in an earlier attempt whose response was lost (crash, timeout, pause).
        existing = c.find_access_profiles(name)
        if existing:
            if (item.get("result") or {}).get("in_doubt"):
                return Outcome("succeeded", existing[0]["id"],
                               "Found the profile from an earlier interrupted attempt", {"reconciled": True})
            return Outcome("skipped", existing[0]["id"], "Already exists; created by someone else after planning")
        group_ids, created_groups = ensure_new_groups(ctx, specs, (item.get("result") or {}).get("in_doubt"))
        for scheme in payload["accessRequestConfig"]["approvalSchemes"]:
            if "newGroup" in scheme:
                scheme["approverId"] = group_ids[scheme.pop("newGroup").lower()]
        try:
            created = c.create_access_profile(payload)
        except ConnectorError as exc:
            if exc.ambiguous:
                # The request may have gone through. Look before declaring failure.
                again = c.find_access_profiles(name)
                if again:
                    return Outcome("succeeded", again[0]["id"], "Created (confirmed after an unclear response)",
                                   {"reconciled": True})
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            raise ItemFailed(str(exc), retryable=exc.retryable, in_doubt=exc.ambiguous) from None
        return Outcome("succeeded", created.get("id"), "Created" + groups_note(created_groups),
                       {"id": created.get("id"), "created_groups": created_groups})

    def rollback_row(self, item):
        if item["status"] == "succeeded" and item.get("remote_id"):
            return {"id": item["remote_id"], "name": item["payload"]["name"]}
        return None


def _resolve_owner(ctx, ref, m):
    """Owners of access profiles are identities. Explains the rule when a governance group is given."""
    before = len(m)
    owner = _lookup_one(ctx, "identity", ref, lambda: ctx.connector.find_identities(ref), "owner", "Owner", m)
    if owner is None and len(m) > before and m[-1]["code"] == "not_found":
        try:
            groups = ctx.cached(("governance_group", ref.lower()), lambda: ctx.connector.find_governance_groups(ref))
        except ConnectorError:
            groups = []
        if groups:
            m[-1] = msg("error", f"'{ref}' is a governance group. Access profile owners must be individual identities; "
                                 f"set a person as owner and add the group as an approver "
                                 f"(approval_schemes: governance_group:{groups[0]['name']}).", "owner",
                        "owner_is_group")
    return owner


def _resolve_group(ctx, ref, m, field_name="approval_schemes", new_groups=None):
    """Resolves a governance group used as approver. When it does not exist and the row fills in
    new_group_owner, returns a placeholder {"id": None, "name": ..., "new": True} for a group to create."""
    if not ref:
        m.append(msg("error", "governance_group: needs a group name or ID", field_name, "invalid"))
        return None
    try:
        found = ctx.cached(("governance_group", ref.lower()), lambda: ctx.connector.find_governance_groups(ref))
    except (AuthError, ConfigError):
        raise
    except ConnectorError as exc:
        m.append(msg("error", f"Could not look up governance group '{ref}': {exc}", field_name, "lookup_failed"))
        return None
    if not found:
        if new_groups is not None and new_groups.enabled:
            return new_groups.plan(ref, field_name)
        hint = " Fill in new_group_owner (and new_group_members) to create it with this row," \
            if new_groups is not None else ""
        m.append(msg("error", f"Governance group '{ref}' does not exist.{hint} Create it first — use "
                              f"\"Create missing governance groups\" on this job — or correct the name.",
                     field_name, "gg_missing", value=ref))
        return None
    if len(found) > 1:
        m.append(msg("error", f"Governance group '{ref}' matches {len(found)} groups; use its ID", field_name,
                     "ambiguous"))
        return None
    group = found[0]
    if group.get("member_count") == 0:
        m.append(msg("warning", f"Governance group '{group['name']}' has no members, so approvals assigned to it "
                                "can never be completed", field_name, "gg_empty"))
    else:
        m.append(msg("info", f"Approver group '{group['name']}' ({group['id']})", field_name))
    return group


class NewGroups:
    """Governance groups a row asks to create (new_group_* columns) because its approval_schemes name groups
    that do not exist yet. Planning only resolves the owner and members; the groups are created when the row
    runs, just before the object that uses them."""

    def __init__(self, ctx, row, m):
        self.ctx, self.row, self.m = ctx, row, m
        self.enabled = bool(row.get("new_group_owner"))
        self._definition = None
        self._resolved = False
        self._planned = {}

    def definition(self):
        """Owner, members and description from the row, resolved once; None if any of them is invalid."""
        if self._resolved:
            return self._definition
        self._resolved = True
        c = self.ctx.connector
        ref = self.row["new_group_owner"]
        owner = _lookup_one(self.ctx, "identity", ref, lambda: c.find_identities(ref), "new_group_owner",
                            "New group owner", self.m)
        members, seen, ok = [], set(), owner is not None
        for r in split_list(self.row.get("new_group_members")):
            ident = _lookup_one(self.ctx, "identity", r, lambda r=r: c.find_identities(r), "new_group_members",
                                "New group member", self.m)
            if ident is None:
                ok = False
            elif ident["id"] not in seen:
                seen.add(ident["id"])
                members.append({"type": "IDENTITY", "id": ident["id"], "name": ident.get("name")})
        if ok:
            self._definition = {"owner": {"type": "IDENTITY", "id": owner["id"], "name": owner.get("name")},
                                "members": members, "description": self.row.get("new_group_description") or None}
        return self._definition

    def plan(self, name, field_name):
        if name.lower() in self._planned:
            return self._planned[name.lower()]
        if re.fullmatch(r"[0-9a-f]{32}", name):
            self.m.append(msg("error", f"Governance group ID '{name}' was not found; only groups given by name can be "
                                       "created", field_name, "not_found"))
            return None
        if len(name) > MAX_GROUP_NAME:
            self.m.append(msg("error", f"Governance group name '{name}' is longer than {MAX_GROUP_NAME} characters",
                              field_name, "too_long"))
            return None
        d = self.definition()
        if d is None:
            return None  # the owner or member errors are already reported
        spec = {"group": {"name": name, "description": d["description"], "owner": d["owner"]},
                "members": d["members"]}
        # Every row of a job that creates the same group must describe it the same way.
        first = self.ctx.cached(("new_governance_group", name.lower()), lambda: spec)
        if _spec_signature(first) != _spec_signature(spec):
            self.m.append(msg("error", f"Another row creates governance group '{name}' with a different owner, "
                                       "members or description; use the same new_group_* values on every row that "
                                       "names it", "new_group_owner", "gg_conflict"))
            return None
        self.m.append(msg("info", f"Governance group '{name}' does not exist; it will be created when this row runs "
                                  f"(owner {d['owner']['name'] or d['owner']['id']}, {len(d['members'])} member(s))",
                          field_name, "gg_create", value=name))
        if not d["members"]:
            self.m.append(msg("warning", f"The new group '{name}' has no members, so approvals assigned to it can "
                                         "never be completed", "new_group_members", "gg_empty"))
        group = {"id": None, "name": name, "new": True, "spec": spec}
        self._planned[name.lower()] = group
        return group

    def specs(self):
        return [g["spec"] for g in self._planned.values()]


def _spec_signature(spec):
    return (spec["group"]["owner"]["id"], spec["group"]["description"], sorted(mb["id"] for mb in spec["members"]))


def ensure_new_groups(ctx, specs, in_doubt=False, create=True):
    """Creates the governance groups planned for a row, or uses them when they already exist (created by an
    earlier row of the job, an earlier interrupted attempt, or someone else). Returns ({lowercase name: id},
    [{"id", "name"} of the groups this call created]). With create=False, missing groups are left out."""
    c = ctx.connector
    ids, created = {}, []
    for spec in specs or []:
        group, member_ids = spec["group"], [mb["id"] for mb in spec["members"]]
        try:
            found = c.find_governance_groups(group["name"])
            if len(found) > 1:
                raise ItemFailed(f"Governance group '{group['name']}' now matches {len(found)} groups; re-plan")
            if found:
                group_id = found[0]["id"]
                if in_doubt:
                    # An earlier attempt may have created the group but not finished adding its members.
                    present = {x["id"] for x in c.list_governance_group_members(group_id)}
                    missing = [i for i in member_ids if i not in present]
                    problems = c.add_governance_group_members(group_id, missing) if missing else []
                    if problems:
                        raise ItemFailed(f"Some members of governance group '{group['name']}' were not added: "
                                         f"{'; '.join(problems)}", in_doubt=True)
            elif not create:
                continue
            else:
                try:
                    new = c.create_governance_group(group)
                except ConnectorError as exc:
                    again = c.find_governance_groups(group["name"]) if exc.ambiguous else []
                    if not again:
                        raise
                    new = again[0]
                group_id = new["id"]
                created.append({"id": group_id, "name": group["name"]})
                problems = c.add_governance_group_members(group_id, member_ids) if member_ids else []
                if problems:
                    raise ItemFailed(f"Governance group '{group['name']}' created ({group_id}) but some members were "
                                     f"not added: {'; '.join(problems)}", in_doubt=True)
        except ConnectorError as exc:
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            # Marked in doubt so the retry finds groups created so far and only completes them.
            raise ItemFailed(f"Could not create governance group '{group['name']}': {exc}",
                             retryable=exc.retryable, in_doubt=True) from None
        ids[group["name"].lower()] = group_id
    return ids, created


def groups_note(created):
    return f"; created governance group(s) {', '.join(g['name'] for g in created)}" if created else ""


def _lookup_one_or_none(ctx, name, m):
    try:
        found = ctx.cached(("access_profile", name.lower()), lambda: ctx.connector.find_access_profiles(name))
    except (AuthError, ConfigError):
        raise
    except ConnectorError as exc:
        m.append(msg("error", f"Could not check for an existing profile: {exc}", "name", "lookup_failed"))
        return None
    return found[0] if found else None


class DeleteAccessProfiles(Action):
    key = "access_profile.delete"
    title = "Delete access profiles"
    description = ("Deletes access profiles by ID (preferred) or exact name. Used for rollback of a create job. "
                   "Profiles still used by roles cannot be deleted and are reported as failures.")
    columns = [
        Column("id", False, "Access profile ID. Takes precedence over name.", "2c9180857d0a6b0e017d0b3e5f1a0042"),
        Column("name", False, "Exact access profile name (used when id is blank).", "Finance - Read"),
    ]

    def duplicate_key(self, row):
        return (row.get("id") or row.get("name") or "").strip().lower() or None

    def plan(self, ctx, row, options):
        c = ctx.connector
        m = []
        profile_id, name = row.get("id", ""), row.get("name", "")
        if not profile_id and not name:
            return Plan("error", "", None, [msg("error", "Either id or name is required", "id", "required")])
        try:
            if profile_id:
                found = c.get_access_profile(profile_id)
                profile = {"id": found["id"], "name": found["name"]} if found else None
                if profile and name and profile["name"] != name:
                    m.append(msg("warning", f"ID belongs to '{profile['name']}', not '{name}'", "name"))
            else:
                matches = c.find_access_profiles(name)
                if len(matches) > 1:
                    return Plan("error", name, None, [msg("error", f"'{name}' matches {len(matches)} profiles; "
                                                                   "use the ID", "name", "ambiguous")])
                profile = matches[0] if matches else None
        except (AuthError, ConfigError):
            raise
        except ConnectorError as exc:
            return Plan("error", profile_id or name, None,
                        [msg("error", f"Lookup failed: {exc}", "id", "lookup_failed")])
        if not profile:
            m.append(msg("info", "Not found (already deleted?); this row will be skipped", "id", "missing"))
            return Plan("skip", profile_id or name, None, m)
        return Plan("delete", profile["name"], {"id": profile["id"], "name": profile["name"]}, m)

    def execute(self, ctx, item, options):
        profile_id = item["payload"]["id"]
        try:
            ctx.connector.delete_access_profile(profile_id)
        except ConnectorError as exc:
            if exc.status == 404:
                return Outcome("skipped", profile_id, "Already gone")
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            raise ItemFailed(str(exc), retryable=exc.retryable) from None
        return Outcome("succeeded", profile_id, "Deleted")
