import pytest
import requests

from amtoolkit import csvio
from amtoolkit.actions import get_action
from amtoolkit.connectors.base import ConnectorError, Interrupted
from amtoolkit.connectors.http import ApiClient, RateLimiter
from amtoolkit.connectors.sailpoint import q

CREATE = get_action("access_profile.create")


def test_csv_parse_semicolon_bom_and_header_case():
    data = "﻿Name;OWNER;Source;Entitlements;extra\nA;alice;AD;X|Y\n;;;;\n".encode("utf-8")
    rows, warnings = csvio.parse(data, CREATE)
    assert len(rows) == 1
    line, row = rows[0]
    assert line == 2 and row["name"] == "A" and row["entitlements"] == "X|Y"
    assert any("extra" in w for w in warnings)


def test_csv_missing_required_columns():
    with pytest.raises(csvio.CsvError, match="Missing required"):
        csvio.parse(b"name,owner\nA,b\n", CREATE)


def test_export_neutralises_formulas():
    out = csvio.write(["a"], [["=HYPERLINK(\"x\")"], ["@SUM(1)"], ["ok"]])
    assert "'=HYPERLINK" in out and "'@SUM" in out and "\r\nok\r\n" in out


def test_filter_quoting():
    assert q('a"b\\c') == '"a\\"b\\\\c"'


class FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = b"x" if body is not None else b""
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeToken:
    def get(self):
        return "t"

    def invalidate(self):
        pass


def client(responses, **kw):
    session = FakeSession(responses)
    return ApiClient("https://x.example", FakeToken(), session=session, backoff_base=0, **kw), session


def test_retries_429_with_retry_after():
    api, s = client([FakeResp(429, {}, {"Retry-After": "0"}), FakeResp(200, [1])])
    assert api.request("GET", "/a") == [1]
    assert len(s.calls) == 2


def test_post_timeout_is_ambiguous_and_not_retried():
    api, s = client([requests.ReadTimeout(), FakeResp(201, {"id": "1"})])
    with pytest.raises(ConnectorError) as exc:
        api.request("POST", "/a", json={})
    assert exc.value.ambiguous and len(s.calls) == 1


def test_get_5xx_retried():
    api, s = client([FakeResp(502, {}), FakeResp(200, {"ok": 1})])
    assert api.request("GET", "/a") == {"ok": 1}


def test_4xx_not_retried_and_message_extracted():
    api, s = client([FakeResp(400, {"messages": [{"text": "name is invalid"}]})])
    with pytest.raises(ConnectorError, match="name is invalid"):
        api.request("POST", "/a", json={})
    assert len(s.calls) == 1


def test_abort_interrupts_backoff():
    import threading
    ev = threading.Event()
    ev.set()
    api, _ = client([FakeResp(200, {})], abort_event=ev)
    with pytest.raises(Interrupted):
        api.request("GET", "/a")


def test_rate_limiter_blocks_after_capacity():
    rl = RateLimiter(2, 10)
    assert rl.wait_time() == 0 and rl.wait_time() == 0
    assert rl.wait_time() > 0


def test_http_only_https():
    with pytest.raises(ConnectorError):
        ApiClient("http://insecure", FakeToken())
