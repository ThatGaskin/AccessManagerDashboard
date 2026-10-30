"""Action contract. An action turns one CSV row into one planned, reviewable API change and executes it.

  normalize(row)        -> canonical dict of the action's columns
  plan(ctx, row, opts)  -> Plan: what would be sent and why (read-only lookups only, never changes anything)
  execute(ctx, item)    -> Outcome: performs the planned change; must be safe to call again after a crash
  rollback_row(item)    -> input row for the inverse action, used to build undo jobs
"""
from dataclasses import dataclass, field


@dataclass
class Column:
    name: str
    required: bool = False
    help: str = ""
    example: str = ""

    def to_dict(self):
        return {"name": self.name, "required": self.required, "help": self.help, "example": self.example}


@dataclass
class Option:
    name: str
    label: str
    default: str
    choices: list
    help: str = ""

    def to_dict(self):
        return {"name": self.name, "label": self.label, "default": self.default, "choices": self.choices,
                "help": self.help}


@dataclass
class Plan:
    op: str  # create | delete | skip | error
    target: str = ""
    payload: dict = None
    messages: list = field(default_factory=list)


@dataclass
class Outcome:
    status: str  # succeeded | skipped
    remote_id: str = None
    message: str = ""
    result: dict = None


class ItemFailed(Exception):
    def __init__(self, message, retryable=False, in_doubt=False):
        super().__init__(message)
        self.retryable = retryable
        # True when the change may have been applied despite the error; the next attempt reconciles.
        self.in_doubt = in_doubt


def msg(level, text, field_name=None, code=None, value=None):
    m = {"level": level, "text": text, "field": field_name, "code": code}
    if value is not None:
        m["value"] = value  # the unresolved reference, e.g. a missing governance group name
    return m


class PlanContext:
    """Connector plus a lookup cache, so 500 rows sharing one source resolve it once."""

    def __init__(self, connector):
        self.connector = connector
        self._cache = {}

    def cached(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]


class Action:
    key = ""
    title = ""
    description = ""
    columns: list = []
    options: list = []
    inverse = None  # key of the action that undoes this one

    def column_names(self):
        return [c.name for c in self.columns]

    def default_options(self):
        return {o.name: o.default for o in self.options}

    def clean_options(self, raw):
        opts = self.default_options()
        for o in self.options:
            value = (raw or {}).get(o.name)
            if value in o.choices:
                opts[o.name] = value
        return opts

    def normalize(self, row: dict) -> dict:
        """Keeps only known columns (matched case-insensitively, spaces/dashes ignored) and trims values."""
        lookup = {self._norm(name): name for name in self.column_names()}
        out = {name: "" for name in self.column_names()}
        for key, value in row.items():
            canonical = lookup.get(self._norm(key or ""))
            if canonical:
                out[canonical] = (value or "").strip()
        return out

    @staticmethod
    def _norm(name):
        return name.strip().lower().replace(" ", "_").replace("-", "_")

    def duplicate_key(self, row):
        """Rows sharing the same key in one job are flagged as duplicates."""
        return None

    def plan(self, ctx: PlanContext, row: dict, options: dict) -> Plan:
        raise NotImplementedError

    def execute(self, ctx: PlanContext, item: dict, options: dict) -> Outcome:
        raise NotImplementedError

    def rollback_row(self, item: dict):
        return None

    def to_dict(self):
        return {"key": self.key, "title": self.title, "description": self.description,
                "columns": [c.to_dict() for c in self.columns], "options": [o.to_dict() for o in self.options],
                "inverse": self.inverse}
