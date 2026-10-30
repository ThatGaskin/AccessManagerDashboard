"""References from CSV cells to objects in the target system: local validation and batched lookups.

Columns declare what they refer to (`Column(ref=...)`). Before a row is planned its references are checked
locally — a malformed email or ID fails the row without any API call. Before a job is planned, the values
of all its rows are looked up together (`prefetch`), several per request where the connector supports it,
and stored in the plan cache under the same keys the per-row lookups use. Per-row planning then finds
them in the cache, so a 500-row file needs a handful of requests instead of one per cell.
"""
import logging
import re

from ..connectors.base import AuthError, ConfigError, ConnectorError
from ..util import split_list
from .base import msg

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.I)
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_REF = 255

# Reference kinds. "object:<type>" uses the generic object lookup; "entitlement" needs a source scope;
# "approver" is an approval_schemes cell (manager, owner, … or governance_group:<name>).
LABELS = {"identity": "Identity", "source": "Source", "access_profile": "Access profile", "entitlement": "Entitlement",
          "governance_group": "Governance group", "id": "ID", "object:access_profile": "Access profile",
          "object:role": "Role", "object:entitlement": "Entitlement", "object:governance_group": "Governance group",
          "object:source": "Source", "approver": "Approver"}


def is_id(value):
    return bool(ID_RE.match(value.strip()))


def cache_key(kind, value, scope=None):
    """The plan-cache key the per-row lookups use for this reference (see access_profiles._lookup_one)."""
    v = value.strip().lower()
    if kind.startswith("object:"):
        return ("object", kind.split(":", 1)[1], scope), v
    if kind == "entitlement":
        return "entitlement", (scope, v)
    return kind, v


def check_value(kind, value, field_name):
    """Local check of one reference; returns an error message or None. Makes no API call. Deliberately
    lenient: any text is accepted except what can never match (control characters, over-long values, a
    non-ID in an ID-only column); everything else is looked up, with "did you mean" hints on misses."""
    label = LABELS.get(kind, "Reference")
    if CONTROL_RE.search(value):
        return msg("error", f"{label} '{value}' contains control characters (line break, tab…)", field_name,
                   "invalid_ref", value=value)
    if len(value) > MAX_REF:
        return msg("error", f"{label} is longer than {MAX_REF} characters", field_name, "invalid_ref")
    if kind == "id" and not is_id(value):
        return msg("error", f"'{value}' is not an ID (32 hexadecimal characters)", field_name, "invalid_ref",
                   value=value)
    return None


def check_row(columns, row):
    """Validates every reference column of a row. Returns messages (errors and warnings)."""
    out = []
    for col in columns:
        if not col.ref or not row.get(col.name):
            continue
        values = split_list(row[col.name]) if col.multi else [row[col.name]]
        seen = set()
        for value in values:
            problem = check_value(col.ref, value, col.name)
            if problem:
                out.append(problem)
            elif value.lower() in seen:
                out.append(msg("warning", f"'{value}' is listed twice in {col.name}", col.name, "duplicate_ref"))
            seen.add(value.lower())
    return out


def check_group_name(value, field_name):
    """governance_group:<name> inside approval_schemes."""
    return check_value("governance_group", value, field_name) if value else None


# ---------------------------------------------------------------------------- batched lookups
def prefetch(ctx, refs):
    """Looks up many references with as few requests as possible and stores them in the plan cache.

    refs: iterable of (kind, value, scope) where scope is a source *name* for entitlements (or None).
    Sources are resolved first because scoped lookups need their IDs. Lookup failures are not fatal:
    whatever is not prefetched is simply looked up row by row later.
    """
    refs = [(k, v.strip(), s) for k, v, s in refs if v and v.strip() and check_value(k, v.strip(), None) is None
            and k != "id"]
    if not refs:
        return 0
    wanted = {}
    for kind, value, scope in refs:
        if scope is not None:
            wanted.setdefault(("source", None), {})[cache_key("source", scope)] = scope
        else:
            wanted.setdefault((kind, None), {})[cache_key(kind, value)] = value
    fetched = _fetch_all(ctx, wanted)
    scoped = {}
    for kind, value, scope in refs:
        if scope is None:
            continue
        sources = ctx.peek(cache_key("source", scope))
        if sources and len(sources) == 1:
            sid = sources[0]["id"]
            scoped.setdefault((kind, sid), {})[cache_key(kind, value, sid)] = value
    fetched += _fetch_all(ctx, scoped)
    return fetched


def _fetch_all(ctx, wanted):
    count = 0
    for (kind, scope), keys in wanted.items():
        todo = {k: v for k, v in keys.items() if not ctx.has(k)}
        if not todo:
            continue
        try:
            found = ctx.connector.find_many(kind, list(dict.fromkeys(todo.values())), scope)
        except (AuthError, ConfigError):
            raise
        except ConnectorError as exc:
            log.info("Batched lookup of %d %s reference(s) failed (%s); they are looked up one by one", len(todo),
                     kind, exc)
            continue
        for key, value in todo.items():
            if value.lower() in found:
                ctx.remember(key, found[value.lower()])
                count += 1
        log.debug("Prefetched %d of %d %s reference(s)", sum(v.lower() in found for v in todo.values()), len(todo),
                  kind)
    return count
