"""Registry of bulk actions. Add a new action by subclassing `Action` and listing it here."""
from .access_profiles import CreateAccessProfiles, DeleteAccessProfiles
from . import references  # noqa: F401
from .copy import CopyObjects, RestoreObjects
from .base import Action, ItemFailed, Outcome, Plan, PlanContext, msg  # noqa: F401
from .experimental import (CancelAccessRequests, ChangeGroupMembers, CreateRoles, DeleteRoles, ForwardApprovals,
                           ReassignOwnership, SubmitAccessRequests, UpdateAccessProfiles, UpdateEntitlements)
from .governance_groups import CreateGovernanceGroups, DeleteGovernanceGroups

ACTIONS = {a.key: a for a in (
    CreateAccessProfiles(), DeleteAccessProfiles(), CreateGovernanceGroups(), DeleteGovernanceGroups(),
    # experimental
    UpdateAccessProfiles(), ReassignOwnership(), UpdateEntitlements(), ChangeGroupMembers(), CreateRoles(),
    DeleteRoles(), SubmitAccessRequests(), CancelAccessRequests(), ForwardApprovals(), CopyObjects(),
    RestoreObjects(),
)}


def get_action(key) -> Action:
    if key not in ACTIONS:
        raise KeyError(f"Unknown action '{key}'")
    return ACTIONS[key]
