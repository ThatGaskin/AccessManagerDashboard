"""Connector registry and the per-connection runtime state (secrets, tokens, rate limiters)."""
import functools
import logging
import os
import threading
import time
from pathlib import Path

from .base import AuthError, ConfigError, Connector, ConnectorError, Interrupted  # noqa: F401
from .http import ClientCredentialsToken, RateLimiter
from .mock import MockTenant
from .sailpoint import SailPointISC

log = logging.getLogger(__name__)

KINDS = {cls.kind: cls for cls in (SailPointISC, MockTenant)}
ENVIRONMENTS = ("sandbox", "test", "production")


def kinds_metadata():
    return [{"kind": cls.kind, "label": cls.label, "description": cls.description,
             "needs_secret": cls.needs_secret, "fields": [f.to_dict() for f in cls.fields]}
            for cls in KINDS.values()]


class LoggedConnector:
    """Wraps a connector and logs every call at DEBUG: name, arguments, result size and duration. Arguments
    are names, IDs and payloads of the objects being changed; secrets never pass through connector methods."""

    def __init__(self, connector, label):
        self._connector = connector
        self._label = label

    def __getattr__(self, name):
        attr = getattr(self._connector, name)
        if name.startswith("_") or not callable(attr) or not log.isEnabledFor(logging.DEBUG):
            return attr

        @functools.wraps(attr)
        def call(*args, **kwargs):
            started = time.monotonic()
            shown = ", ".join([_short(a) for a in args] + [f"{k}={_short(v)}" for k, v in kwargs.items()])
            try:
                result = attr(*args, **kwargs)
            except Exception as exc:
                log.debug("[%s] %s(%s) raised %s: %s (%.0f ms)", self._label, name, shown, exc.__class__.__name__,
                          exc, (time.monotonic() - started) * 1000)
                raise
            size = f"{len(result)} result(s)" if isinstance(result, list) else \
                "found" if isinstance(result, dict) else "none" if result is None else _short(result)
            log.debug("[%s] %s(%s) -> %s (%.0f ms)", self._label, name, shown, size,
                      (time.monotonic() - started) * 1000)
            return result
        return call


def _short(value, limit=80):
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


class ConnectionManager:
    """Builds connectors and owns things that must outlive a single job: secrets kept in memory,
    cached OAuth tokens, and one rate limiter per connection (shared by every job using it)."""

    def __init__(self, store, data_dir: Path):
        self.store = store
        self.data_dir = Path(data_dir)
        self._secrets = {}
        self._tokens = {}
        self._limiters = {}
        self._lock = threading.Lock()

    def set_secret(self, conn_id, secret):
        with self._lock:
            self._secrets[conn_id] = secret
            self._tokens.pop(conn_id, None)

    def clear_secret(self, conn_id):
        with self._lock:
            self._secrets.pop(conn_id, None)
            self._tokens.pop(conn_id, None)

    def _secret(self, conn):
        if conn["id"] in self._secrets:
            return self._secrets[conn["id"]]
        env = (conn["config"].get("secret_env") or "").strip()
        return os.environ.get(env) if env else None

    def secret_status(self, conn):
        cls = KINDS.get(conn["kind"])
        if cls is None or not cls.needs_secret:
            return "not_required"
        if conn["id"] in self._secrets:
            return "memory"
        env = (conn["config"].get("secret_env") or "").strip()
        if env and os.environ.get(env):
            return "env"
        return "missing"

    def forget(self, conn_id):
        with self._lock:
            for cache in (self._secrets, self._tokens, self._limiters):
                cache.pop(conn_id, None)

    def build(self, conn_id, abort_event=None) -> Connector:
        conn = self.store.get_connection(conn_id)
        cls = KINDS.get(conn["kind"])
        if cls is None:
            raise ConfigError(f"Unknown connection type '{conn['kind']}'")
        cfg = conn["config"]
        if cls is MockTenant:
            return LoggedConnector(MockTenant(cfg, self.data_dir / f"sandbox_{conn_id}.json",
                                              abort_event=abort_event), conn["name"])
        with self._lock:
            limiter = self._limiters.get(conn_id)
            if limiter is None:
                try:
                    calls = max(1, int(cfg.get("max_calls_per_10s") or 80))
                except ValueError:
                    calls = 80
                limiter = self._limiters[conn_id] = RateLimiter(calls, 10)
            token = self._tokens.get(conn_id)
            base = (cfg.get("base_url") or "").strip().rstrip("/")
            if token is None or token.client_id != cfg.get("client_id") or not token.token_url.startswith(base):
                token = self._tokens[conn_id] = ClientCredentialsToken(
                    f"{base}/oauth/token", cfg.get("client_id"), lambda: self._secret(self.store.get_connection(conn_id)))
        return LoggedConnector(cls(cfg, None, token=token, limiter=limiter, abort_event=abort_event), conn["name"])
