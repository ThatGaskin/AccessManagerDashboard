"""Connector contract: every target system exposes the same small, domain-level interface.

Actions talk to connectors, never to HTTP directly, so the same CSV workflow can run against a real tenant
or the offline sandbox.
"""


class ConnectorError(Exception):
    """An API call failed.

    retryable  – trying again later may succeed (rate limit, outage).
    ambiguous  – the request may or may not have been applied (e.g. a timeout after sending a POST);
                 callers must check the remote state before retrying.
    """

    def __init__(self, message, *, status=None, retryable=False, ambiguous=False, details=None):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.details = details


class AuthError(ConnectorError):
    """Credentials are missing or rejected. Stops the whole job rather than failing item after item."""


class ConfigError(ConnectorError):
    """The connection is misconfigured. Stops the whole job."""


class Interrupted(Exception):
    """The user paused or cancelled while a call was waiting to retry."""


class Field:
    def __init__(self, name, label, *, required=False, default="", help="", choices=None):
        self.name = name
        self.label = label
        self.required = required
        self.default = default
        self.help = help
        self.choices = choices

    def to_dict(self):
        return {"name": self.name, "label": self.label, "required": self.required, "default": self.default,
                "help": self.help, "choices": self.choices}


class Connector:
    kind = ""
    label = ""
    description = ""
    fields: list = []
    needs_secret = True

    # --- connectivity
    def test(self) -> str:
        """Returns a human-readable success message or raises ConnectorError."""
        raise NotImplementedError

    # --- lookups (return None when nothing matches, a list when several do)
    def find_identities(self, ref: str) -> list:
        raise NotImplementedError

    def search_identities(self, query: str, limit: int = 20) -> list:
        """Identities whose name, username (alias) or email starts with / matches `query`, for pickers."""
        raise NotImplementedError

    def find_sources(self, name: str) -> list:
        raise NotImplementedError

    def find_entitlements(self, source_id: str, ref: str) -> list:
        raise NotImplementedError

    def find_access_profiles(self, name: str) -> list:
        raise NotImplementedError

    def get_access_profile(self, profile_id: str):
        raise NotImplementedError

    def find_governance_groups(self, ref: str) -> list:
        """Governance groups (ISC "workgroups") by ID or exact name."""
        raise NotImplementedError

    # --- changes
    def create_access_profile(self, payload: dict) -> dict:
        raise NotImplementedError

    def delete_access_profile(self, profile_id: str) -> None:
        raise NotImplementedError

    def create_governance_group(self, payload: dict) -> dict:
        raise NotImplementedError

    def add_governance_group_members(self, group_id: str, identity_ids: list) -> list:
        """Adds members; returns a list of human-readable problems (empty when all were added)."""
        raise NotImplementedError

    def delete_governance_group(self, group_id: str) -> None:
        raise NotImplementedError

    # --- generic object access (experimental features). Objects are normalized by connectors.model.
    def get_object(self, type_: str, object_id: str):
        raise NotImplementedError

    def find_objects(self, type_: str, ref: str, source_id: str = None) -> list:
        """By ID or exact name (entitlements: name or value, within `source_id` when given)."""
        raise NotImplementedError

    def list_objects(self, type_: str, limit: int = 20000, abort_event=None):
        """Yields every object of a type (paged), up to `limit`."""
        raise NotImplementedError

    def patch_object(self, type_: str, object_id: str, changes: dict) -> None:
        raise NotImplementedError

    def create_role(self, payload: dict) -> dict:
        raise NotImplementedError

    def delete_role(self, role_id: str) -> None:
        raise NotImplementedError

    def list_governance_group_members(self, group_id: str) -> list:
        raise NotImplementedError

    def remove_governance_group_members(self, group_id: str, identity_ids: list) -> list:
        raise NotImplementedError

    def find_access_requests(self, identity_id: str) -> list:
        """Open (pending) access requests for an identity: [{id, state, request_type, item_id, item_name}]."""
        raise NotImplementedError

    def get_access_request(self, request_id: str):
        raise NotImplementedError

    def submit_access_request(self, payload: dict) -> dict:
        raise NotImplementedError

    def cancel_access_request(self, request_id: str, comment: str) -> None:
        raise NotImplementedError

    def get_pending_approval(self, approval_id: str):
        """{id, approver_id, approver_name, requested_for, item_name} or None when not pending."""
        raise NotImplementedError

    def forward_approval(self, approval_id: str, new_owner_id: str, comment: str) -> None:
        raise NotImplementedError
