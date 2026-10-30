"""Bulk create and bulk delete of access profiles."""
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
        messages.append(msg("error", f"{label} '{shown}' was not found", field_name, "not_found"))
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
        Column("owner", True, "Owner identity: email, username (alias) or identity ID.", "alice@example.com"),
        Column("source", True, "Source name exactly as it appears in the tenant.", "Active Directory"),
        Column("entitlements", True, "Entitlement names or values on that source, separated by ';'.",
               "Finance-Read;VPN Users"),
        Column("requestable", False, "yes/no — can users request it? Default yes.", "yes"),
        Column("enabled", False, "yes/no — default yes.", "yes"),
        Column("approval_schemes", False,
               "Approvers in order, separated by ';': manager, owner, source_owner, app_owner, "
               "governance_group:<id>. Blank = no approval.", "manager;owner"),
        Column("comments_required", False, "yes/no — requester must comment. Default no.", "no"),
        Column("denial_comments_required", False, "yes/no — approver must comment on denial. Default no.", "no"),
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
        for part in split_list(row.get("approval_schemes")):
            low = part.lower().replace(" ", "_")
            if low.startswith("governance_group:"):
                group_id = part.split(":", 1)[1].strip()
                if group_id:
                    schemes.append({"approverType": "GOVERNANCE_GROUP", "approverId": group_id})
                    continue
            if low in APPROVER_TYPES:
                schemes.append({"approverType": APPROVER_TYPES[low], "approverId": None})
            else:
                m.append(msg("error", f"Unknown approver '{part}'", "approval_schemes", "invalid"))
        if flags.get("requestable") and not schemes:
            m.append(msg("warning", "Requestable without approval: requests are granted automatically",
                         "approval_schemes"))

        owner = source = None
        if not row.get("owner"):
            m.append(msg("error", "Owner is required", "owner", "required"))
        else:
            owner = _lookup_one(ctx, "identity", row["owner"], lambda: c.find_identities(row["owner"]),
                                "owner", "Owner", m)
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
        return Plan("create", name, payload, m)

    def execute(self, ctx, item, options):
        c = ctx.connector
        payload = item["payload"]
        name = payload["name"]
        # Idempotency: check first. Catches profiles created by someone else since planning, and
        # profiles we created in an earlier attempt whose response was lost (crash, timeout, pause).
        existing = c.find_access_profiles(name)
        if existing:
            if (item.get("result") or {}).get("in_doubt"):
                return Outcome("succeeded", existing[0]["id"],
                               "Found the profile from an earlier interrupted attempt", {"reconciled": True})
            return Outcome("skipped", existing[0]["id"], "Already exists; created by someone else after planning")
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
        return Outcome("succeeded", created.get("id"), "Created", {"id": created.get("id")})

    def rollback_row(self, item):
        if item["status"] == "succeeded" and item.get("remote_id"):
            return {"id": item["remote_id"], "name": item["payload"]["name"]}
        return None


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
