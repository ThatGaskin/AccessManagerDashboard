"""Small helpers shared across the toolkit."""
import hashlib
import json
import secrets
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def dumps(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def loads(text, default=None):
    if text is None or text == "":
        return default
    return json.loads(text)


def fingerprint(value) -> str:
    """Stable SHA-256 of any JSON-serialisable value."""
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def parse_bool(value, default=None):
    """Parses CSV-style booleans. Returns `default` for blanks and raises ValueError for junk."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text == "":
        return default
    if text in {"true", "yes", "y", "1", "on"}:
        return True
    if text in {"false", "no", "n", "0", "off"}:
        return False
    raise ValueError(f"'{value}' is not a yes/no value")


def split_list(value) -> list:
    """Splits a multi-value CSV cell. Accepts ';', '|' or newlines as separators."""
    if not value:
        return []
    text = str(value).replace("|", ";").replace("\n", ";")
    return [part.strip() for part in text.split(";") if part.strip()]
