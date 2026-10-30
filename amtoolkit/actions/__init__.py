"""Registry of bulk actions. Add a new action by subclassing `Action` and listing it here."""
from .access_profiles import CreateAccessProfiles, DeleteAccessProfiles
from .base import Action, ItemFailed, Outcome, Plan, PlanContext, msg  # noqa: F401
from .governance_groups import CreateGovernanceGroups, DeleteGovernanceGroups

ACTIONS = {a.key: a for a in (CreateAccessProfiles(), DeleteAccessProfiles(),
                              CreateGovernanceGroups(), DeleteGovernanceGroups())}


def get_action(key) -> Action:
    if key not in ACTIONS:
        raise KeyError(f"Unknown action '{key}'")
    return ACTIONS[key]
