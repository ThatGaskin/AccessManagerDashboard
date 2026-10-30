import time

import pytest

from amtoolkit.app import create_app


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path)
    yield app
    app.extensions["toolkit"].engine.shutdown(timeout=5)


@pytest.fixture
def toolkit(app):
    return app.extensions["toolkit"]


@pytest.fixture
def client(app):
    c = app.test_client()
    c.environ_base["HTTP_X_CSRF_TOKEN"] = app.config["CSRF_TOKEN"]
    c.environ_base["HTTP_HOST"] = "127.0.0.1:8765"
    return c


@pytest.fixture
def sandbox(toolkit):
    return toolkit.save_connection({"name": "Sandbox", "kind": "mock", "environment": "sandbox",
                                    "config": {"latency_ms": "0", "failure_rate": "0"}})


def wait_for(fn, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")
