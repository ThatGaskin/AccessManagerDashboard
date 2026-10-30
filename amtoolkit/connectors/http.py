"""Resilient HTTP client: OAuth client-credentials, client-side rate limiting, retries with backoff.

Retry rules (the important part for bulk changes):
  * 429 and 503 are always retried, honouring Retry-After — the server did not process the request.
  * Connection failures before the request was sent are always retried.
  * GET/PUT/DELETE are idempotent and are retried on other 5xx errors and read timeouts.
  * POST/PATCH are NOT blindly retried after a timeout or 5xx: the change may already have happened.
    They raise an `ambiguous` error and the action reconciles against the remote state instead.
"""
import logging
import random
import threading
import time

import requests

from .base import AuthError, ConfigError, ConnectorError, Interrupted

log = logging.getLogger(__name__)

IDEMPOTENT = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}


class RateLimiter:
    """Token bucket shared by every client of one connection."""

    def __init__(self, max_calls: int, per_seconds: float):
        self.capacity = max_calls
        self.rate = max_calls / per_seconds
        self.tokens = float(max_calls)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def wait_time(self) -> float:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return 0.0
            return (1 - self.tokens) / self.rate


class ClientCredentialsToken:
    """Caches an OAuth2 access token and refreshes it shortly before expiry."""

    def __init__(self, token_url, client_id, secret_provider, session=None, timeout=(10, 30)):
        self.token_url = token_url
        self.client_id = client_id
        self.secret_provider = secret_provider
        self.session = session or requests.Session()
        self.timeout = timeout
        self._token = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def get(self, force=False) -> str:
        with self._lock:
            if not force and self._token and time.monotonic() < self._expires:
                return self._token
            secret = self.secret_provider()
            if not self.client_id or not secret:
                raise AuthError("Client ID or client secret is missing for this connection")
            try:
                resp = self.session.post(self.token_url, data={
                    "grant_type": "client_credentials", "client_id": self.client_id, "client_secret": secret,
                }, timeout=self.timeout)
            except requests.RequestException as exc:
                raise ConnectorError(f"Could not reach token endpoint: {exc.__class__.__name__}",
                                     retryable=True) from None
            if resp.status_code in (400, 401, 403):
                raise AuthError(f"Token request rejected (HTTP {resp.status_code}); check client ID and secret",
                                status=resp.status_code)
            if not resp.ok:
                raise ConnectorError(f"Token request failed (HTTP {resp.status_code})", status=resp.status_code,
                                     retryable=resp.status_code >= 500)
            body = resp.json()
            log.info("Obtained an access token for client %s", self.client_id)
            self._token = body["access_token"]
            self._expires = time.monotonic() + max(30, int(body.get("expires_in", 600)) - 60)
            return self._token

    def invalidate(self):
        with self._lock:
            self._token = None


class ApiClient:
    def __init__(self, base_url, token, *, limiter=None, abort_event=None, session=None,
                 timeout=(10, 60), max_retries=5, backoff_base=1.0, backoff_cap=60.0):
        if not base_url or not base_url.startswith("https://"):
            raise ConfigError("API base URL must start with https://")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.limiter = limiter
        self.abort_event = abort_event
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap

    def _sleep(self, seconds):
        if seconds <= 0:
            return
        if self.abort_event is not None:
            if self.abort_event.wait(seconds):
                raise Interrupted()
        else:
            time.sleep(seconds)

    def _backoff(self, attempt, retry_after=None):
        if retry_after is not None:
            return min(self.backoff_cap, retry_after)
        # Exponential backoff with full jitter.
        return random.uniform(0, min(self.backoff_cap, self.backoff_base * (2 ** attempt)))

    @staticmethod
    def _retry_after(resp):
        value = resp.headers.get("Retry-After")
        try:
            return max(0.0, float(value)) if value is not None else None
        except ValueError:
            return None

    @staticmethod
    def _error_text(resp):
        try:
            body = resp.json()
        except ValueError:
            return resp.text[:300]
        if isinstance(body, dict):
            # SailPoint: {"detailCode":..., "messages":[{"text":...}]}
            msgs = body.get("messages") or []
            texts = [m.get("text") for m in msgs if isinstance(m, dict) and m.get("text")]
            if texts:
                return "; ".join(texts)
            for key in ("message", "error_description", "error", "detailCode"):
                if body.get(key):
                    return str(body[key])
        return str(body)[:300]

    def request(self, method, path, *, params=None, json=None, ok=(200, 201, 202, 204, 207), headers=None):
        method = method.upper()
        idempotent = method in IDEMPOTENT
        url = f"{self.base_url}{path}"
        refreshed = False
        attempt = 0
        while True:
            if self.abort_event is not None and self.abort_event.is_set():
                raise Interrupted()
            if self.limiter is not None:
                self._sleep(self.limiter.wait_time())
            all_headers = {**(headers or {}), "Authorization": f"Bearer {self.token.get()}",
                           "Accept": "application/json"}
            started = time.monotonic()
            try:
                resp = self.session.request(method, url, params=params, json=json, headers=all_headers,
                                            timeout=self.timeout)
            except requests.ConnectTimeout:
                error = ConnectorError("Connection timed out", retryable=True)
                safe_to_retry = True  # nothing was sent
            except requests.ConnectionError as exc:
                # A refused/reset connection may happen after the body was sent; only idempotent calls retry.
                error = ConnectorError(f"Connection error: {exc.__class__.__name__}", retryable=True,
                                       ambiguous=not idempotent)
                safe_to_retry = idempotent
            except requests.Timeout:
                error = ConnectorError("Timed out waiting for the response", retryable=True,
                                       ambiguous=not idempotent)
                safe_to_retry = idempotent
            else:
                log.debug("%s %s -> %s (%.0f ms%s)", method, path, resp.status_code,
                          (time.monotonic() - started) * 1000, f", attempt {attempt + 1}" if attempt else "")
                if resp.status_code in ok:
                    if resp.status_code == 204 or not resp.content:
                        return None
                    return resp.json()
                status = resp.status_code
                text = self._error_text(resp)
                log.info("%s %s -> HTTP %s: %s", method, path, status, text)
                if status == 401 and not refreshed:
                    log.info("Access token rejected; requesting a new one")
                    self.token.invalidate()
                    refreshed = True
                    continue
                if status in (401, 403):
                    raise AuthError(f"HTTP {status}: {text}", status=status)
                if status == 404:
                    raise ConnectorError(f"Not found: {text}", status=404)
                if status in (429, 503):
                    error = ConnectorError(f"HTTP {status}: {text}", status=status, retryable=True)
                    safe_to_retry = True
                    if attempt < self.max_retries:
                        wait = self._backoff(attempt, self._retry_after(resp))
                        log.warning("%s %s throttled (HTTP %s); retrying in %.1f s", method, path, status, wait)
                        self._sleep(wait)
                        attempt += 1
                        continue
                    raise error
                if status >= 500:
                    error = ConnectorError(f"HTTP {status}: {text}", status=status, retryable=True,
                                           ambiguous=not idempotent)
                    safe_to_retry = idempotent
                else:
                    raise ConnectorError(f"HTTP {status}: {text}", status=status)
            if safe_to_retry and attempt < self.max_retries:
                wait = self._backoff(attempt)
                log.warning("%s %s failed (%s); retrying in %.1f s", method, path, error, wait)
                self._sleep(wait)
                attempt += 1
                continue
            log.warning("%s %s failed (%s)%s", method, path, error,
                        "; the change may have been applied" if error.ambiguous else "")
            raise error
