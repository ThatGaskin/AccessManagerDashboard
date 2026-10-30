"""Bulk create and bulk delete of governance groups (called "workgroups" in the ISC API).

Governance groups are teams of identities used as approvers (e.g. in access profile approval schemes)
and reviewers. They are owned by one identity and have any number of identity members.
"""
from ..connectors.base import AuthError, ConfigError, ConnectorError
from ..util import split_list
from .access_profiles import _lookup_one
from .base import Action, Column, ItemFailed, Option, Outcome, Plan, msg

MAX_NAME = 128


class CreateGovernanceGroups(Action):
    key = "governance_group.create"
    title = "Create governance groups"
    description = ("Creates governance groups (approver teams) with an owner and members. Use the groups as "
                   "approvers in access profiles with approval_schemes = governance_group:<name>.")
    inverse = "governance_group.delete"
    columns = [
        Column("name", True, f"Unique group name (max {MAX_NAME} characters).", "Finance Approvers"),
        Column("description", False, "What the group approves or reviews.", "Approves access to finance systems"),
        Column("owner", True, "Owner identity: email, username (alias) or identity ID. Must be a person.",
               "alice@example.com"),
        Column("members", False, "Member identities (email, alias or ID), separated by ';'. A group without "
               "members cannot approve anything.", "alice@example.com;carol"),
    ]
    options = [
        Option("if_exists", "If a group with the same name already exists", "skip", ["skip", "error"],
               "skip = leave the existing group alone; error = flag the row so you must decide."),
    ]

    def duplicate_key(self, row):
        return (row.get("name") or "").strip().lower() or None

    def plan(self, ctx, row, options):
        c = ctx.connector
        m = []
        name = row.get("name", "")
        if not name:
            m.append(msg("error", "Name is required", "name", "required"))
        elif len(name) > MAX_NAME:
            m.append(msg("error", f"Name is longer than {MAX_NAME} characters", "name", "too_long"))
        owner = None
        if not row.get("owner"):
            m.append(msg("error", "Owner is required — a governance group must be owned by a person", "owner",
                         "required"))
        else:
            owner = _lookup_one(ctx, "identity", row["owner"], lambda: c.find_identities(row["owner"]), "owner",
                                "Owner", m)
        members, seen = [], set()
        for ref in split_list(row.get("members")):
            ident = _lookup_one(ctx, "identity", ref, lambda r=ref: c.find_identities(r), "members", "Member", m)
            if ident and ident["id"] not in seen:
                seen.add(ident["id"])
                members.append({"type": "IDENTITY", "id": ident["id"], "name": ident.get("name")})
        if not members and not any(x["level"] == "error" and x["field"] == "members" for x in m):
            m.append(msg("warning", "No members: approvals assigned to this group could never be completed",
                         "members"))

        if name and len(name) <= MAX_NAME:
            try:
                existing = ctx.cached(("governance_group", name.lower()), lambda: c.find_governance_groups(name))
            except (AuthError, ConfigError):
                raise
            except ConnectorError as exc:
                m.append(msg("error", f"Could not check for an existing group: {exc}", "name", "lookup_failed"))
                existing = []
            if existing:
                if options.get("if_exists") == "error":
                    m.append(msg("error", f"A governance group named '{name}' already exists ({existing[0]['id']})",
                                 "name", "exists"))
                else:
                    m.append(msg("info", f"Already exists ({existing[0]['id']}); this row will be skipped", "name",
                                 "exists"))
                    return Plan("skip", name, {"existing_id": existing[0]["id"]}, m)

        if any(x["level"] == "error" for x in m):
            return Plan("error", name, None, m)
        group = {"name": name, "description": row.get("description") or None,
                 "owner": {"type": "IDENTITY", "id": owner["id"], "name": owner.get("name")}}
        return Plan("create", name, {"group": group, "members": members}, m)

    def execute(self, ctx, item, options):
        c = ctx.connector
        group, members = item["payload"]["group"], item["payload"]["members"]
        in_doubt = (item.get("result") or {}).get("in_doubt")
        existing = c.find_governance_groups(group["name"])
        if existing and not in_doubt:
            return Outcome("skipped", existing[0]["id"], "Already exists; created by someone else after planning")
        if existing:
            group_id, note = existing[0]["id"], "Found the group from an earlier interrupted attempt"
        else:
            try:
                created = c.create_governance_group(group)
            except ConnectorError as exc:
                again = c.find_governance_groups(group["name"]) if exc.ambiguous else []
                if not again:
                    if isinstance(exc, (AuthError, ConfigError)):
                        raise
                    raise ItemFailed(str(exc), retryable=exc.retryable, in_doubt=exc.ambiguous) from None
                created = again[0]
            group_id, note = created["id"], "Created"
        try:
            problems = c.add_governance_group_members(group_id, [mb["id"] for mb in members])
        except ConnectorError as exc:
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            # The group exists; mark in doubt so a retry finds it and only adds the members.
            raise ItemFailed(f"Group created ({group_id}) but adding members failed: {exc}. Retry to add them.",
                             retryable=True, in_doubt=True) from None
        if problems:
            raise ItemFailed(f"Group created ({group_id}) but some members were not added: {'; '.join(problems)}",
                             in_doubt=True)
        return Outcome("succeeded", group_id, f"{note} with {len(members)} member(s)",
                       {"id": group_id, "reconciled": bool(existing)})

    def rollback_row(self, item):
        if item["status"] == "succeeded" and item.get("remote_id"):
            return {"id": item["remote_id"], "name": item["payload"]["group"]["name"]}
        return None


class DeleteGovernanceGroups(Action):
    key = "governance_group.delete"
    title = "Delete governance groups"
    description = ("Deletes governance groups by ID (preferred) or exact name. Used for rollback. Groups still "
                   "used as approvers may be rejected by the API and are reported as failures.")
    columns = [
        Column("id", False, "Governance group ID. Takes precedence over name.", "2c9180867a4e5b2a017a4f1d2b3c0001"),
        Column("name", False, "Exact group name (used when id is blank).", "Finance Approvers"),
    ]

    def duplicate_key(self, row):
        return (row.get("id") or row.get("name") or "").strip().lower() or None

    def plan(self, ctx, row, options):
        ref = row.get("id") or row.get("name")
        if not ref:
            return Plan("error", "", None, [msg("error", "Either id or name is required", "id", "required")])
        try:
            found = ctx.connector.find_governance_groups(ref)
        except (AuthError, ConfigError):
            raise
        except ConnectorError as exc:
            return Plan("error", ref, None, [msg("error", f"Lookup failed: {exc}", "id", "lookup_failed")])
        if len(found) > 1:
            return Plan("error", ref, None, [msg("error", f"'{ref}' matches {len(found)} groups; use the ID", "name",
                                                 "ambiguous")])
        if not found:
            return Plan("skip", ref, None, [msg("info", "Not found (already deleted?); this row will be skipped",
                                                "id", "missing")])
        group = found[0]
        m = [msg("warning", f"Has {group['member_count']} member(s); make sure no access profile still uses it "
                            "as an approver", "name")] if group.get("member_count") else []
        return Plan("delete", group["name"], {"id": group["id"], "name": group["name"]}, m)

    def execute(self, ctx, item, options):
        group_id = item["payload"]["id"]
        try:
            ctx.connector.delete_governance_group(group_id)
        except ConnectorError as exc:
            if exc.status == 404:
                return Outcome("skipped", group_id, "Already gone")
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            raise ItemFailed(str(exc), retryable=exc.retryable) from None
        return Outcome("succeeded", group_id, "Deleted")
