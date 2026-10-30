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

    def find_sources(self, name: str) -> list:
        raise NotImplementedError

    def find_entitlements(self, source_id: str, ref: str) -> list:
        raise NotImplementedError

    def find_access_profiles(self, name: str) -> list:
        raise NotImplementedError

    def get_access_profile(self, profile_id: str):
        raise NotImplementedError

    # --- changes
    def create_access_profile(self, payload: dict) -> dict:
        raise NotImplementedError

    def delete_access_profile(self, profile_id: str) -> None:
        raise NotImplementedError
