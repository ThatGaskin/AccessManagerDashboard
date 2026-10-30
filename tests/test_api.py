import io

from tests.conftest import wait_for


def confirm(client, job_id, command, params=None, text=None):
    """Asks for a summary and returns the confirmation a user gives after ticking the checkbox."""
    summary = client.post(f"/api/jobs/{job_id}/confirm", json={"command": command, "params": params or {}}).get_json()
    return {"token": summary["token"], "acknowledged": True, "text": text}


def test_requires_csrf_token(app, client):
    bare = app.test_client()
    r = bare.post("/api/connections", json={}, headers={"Host": "127.0.0.1:8765"})
    assert r.status_code == 403


def test_rejects_foreign_host(client):
    r = client.get("/api/meta", headers={"Host": "evil.example"})
    assert r.status_code == 400


def test_rejects_cross_origin(client):
    r = client.post("/api/connections", json={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_security_headers_and_token_in_page(app, client):
    r = client.get("/")
    assert app.config["CSRF_TOKEN"] in r.get_data(as_text=True)
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]


def test_secret_is_never_returned(client):
    r = client.post("/api/connections", json={
        "name": "ISC", "kind": "sailpoint_isc", "environment": "test", "secret": "s3cr3t",
        "config": {"base_url": "https://acme.api.identitynow.com", "client_id": "abc"}})
    assert r.status_code == 201
    assert "s3cr3t" not in r.get_data(as_text=True)
    assert r.get_json()["secret_status"] == "memory"
    assert "s3cr3t" not in client.get("/api/connections").get_data(as_text=True)


def test_full_flow_over_http(client, toolkit):
    conn = client.post("/api/connections", json={"name": "SB", "kind": "mock",
                                                 "config": {"latency_ms": "0", "failure_rate": "0"}}).get_json()
    csv = b"name,owner,source,entitlements\nWeb AP,alice,Active Directory,VPN Users\n"
    r = client.post("/api/jobs", data={"action": "access_profile.create", "connection_id": conn["id"],
                                       "file": (io.BytesIO(csv), "x.csv")}, content_type="multipart/form-data")
    job_id = r.get_json()["id"]
    wait_for(lambda: client.get(f"/api/jobs/{job_id}").get_json()["status"] == "review")
    preview = client.get(f"/api/jobs/{job_id}/approval").get_json()
    assert preview["ops"] == {"create": 1}
    assert client.post(f"/api/jobs/{job_id}/approve", json={}).status_code == 428
    assert client.post(f"/api/jobs/{job_id}/approve", json={"confirmation": confirm(client, job_id, "approve")}).status_code == 200
    assert client.post(f"/api/jobs/{job_id}/start", json={}).status_code == 428
    assert client.post(f"/api/jobs/{job_id}/start", json={"confirmation": confirm(client, job_id, "start")}).status_code == 200
    wait_for(lambda: client.get(f"/api/jobs/{job_id}").get_json()["status"] == "completed")
    export = client.get(f"/api/jobs/{job_id}/export.csv").get_data(as_text=True)
    assert "succeeded" in export and "Web AP" in export


def test_bad_csv_returns_400(client):
    conn = client.post("/api/connections", json={"name": "SB", "kind": "mock", "config": {}}).get_json()
    r = client.post("/api/jobs", data={"action": "access_profile.create", "connection_id": conn["id"],
                                       "file": (io.BytesIO(b"foo,bar\n1,2\n"), "x.csv")},
                    content_type="multipart/form-data")
    assert r.status_code == 400 and "Missing required" in r.get_json()["error"]
