import io

import pytest

from tests.conftest import wait_for
from tests.test_api import confirm

CSV = b"name,owner,source,entitlements\nC1,alice,Active Directory,VPN Users\nC2,bob,Salesforce,Read Only\n"


def setup_job(client, environment="sandbox", latency="0"):
    conn = client.post("/api/connections", json={"name": f"Conn {environment}", "kind": "mock",
                                                 "environment": environment,
                                                 "config": {"latency_ms": latency, "failure_rate": "0"}}).get_json()
    job_id = client.post("/api/jobs", data={"action": "access_profile.create", "connection_id": conn["id"],
                                            "file": (io.BytesIO(CSV), "x.csv")},
                         content_type="multipart/form-data").get_json()["id"]
    wait_for(lambda: client.get(f"/api/jobs/{job_id}").get_json()["status"] == "review")
    return conn, job_id


def test_summary_describes_the_run(client):
    _, job_id = setup_job(client)
    s = client.post(f"/api/jobs/{job_id}/confirm", json={"command": "approve"}).get_json()
    assert s["title"] == "Approve the plan" and "2 × create" in s["lines"]
    assert s["checkbox"].startswith("I have reviewed this summary") and s["token"]


@pytest.mark.parametrize("bad", [None, {"acknowledged": True}, {"token": "123.abc", "acknowledged": True}])
def test_commands_without_valid_confirmation_are_refused(client, bad):
    _, job_id = setup_job(client)
    r = client.post(f"/api/jobs/{job_id}/approve", json={"confirmation": bad} if bad else {})
    assert r.status_code == 428 and r.get_json()["code"] == "confirmation_required"


def test_checkbox_must_be_ticked(client):
    _, job_id = setup_job(client)
    c = confirm(client, job_id, "approve")
    c["acknowledged"] = False
    assert client.post(f"/api/jobs/{job_id}/approve", json={"confirmation": c}).status_code == 428


def test_confirmation_is_bound_to_the_state_it_described(client):
    _, job_id = setup_job(client)
    c = confirm(client, job_id, "approve")
    item = client.get(f"/api/jobs/{job_id}/items").get_json()["items"][0]
    client.put(f"/api/jobs/{job_id}/items/{item['id']}", json={"input": dict(item["input"], description="changed")})
    r = client.post(f"/api/jobs/{job_id}/approve", json={"confirmation": c})
    assert r.status_code == 428 and "changed" in r.get_json()["error"]


def test_confirmation_is_bound_to_the_command(client):
    _, job_id = setup_job(client)
    c = confirm(client, job_id, "plan")
    assert client.post(f"/api/jobs/{job_id}/approve", json={"confirmation": c}).status_code == 428


def test_confirmation_is_bound_to_parameters(client):
    _, job_id = setup_job(client)
    ids = [i["id"] for i in client.get(f"/api/jobs/{job_id}/items").get_json()["items"]]
    c = confirm(client, job_id, "include", {"ids": ids[:1], "included": False})
    r = client.post(f"/api/jobs/{job_id}/include", json={"ids": ids, "included": False, "confirmation": c})
    assert r.status_code == 428
    r = client.post(f"/api/jobs/{job_id}/include", json={"ids": ids[:1], "included": False, "confirmation": c})
    assert r.status_code == 200 and r.get_json()["changed"] == 1


def test_production_requires_typed_name_to_start(client):
    conn, job_id = setup_job(client, "production")
    s = client.post(f"/api/jobs/{job_id}/confirm", json={"command": "approve"}).get_json()
    assert s["requires_text"] == conn["name"] and any("PRODUCTION" in w for w in s["warnings"])
    c = confirm(client, job_id, "approve", text="wrong")
    assert client.post(f"/api/jobs/{job_id}/approve", json={"confirmation": c}).status_code == 428
    c = confirm(client, job_id, "approve", text=conn["name"])
    assert client.post(f"/api/jobs/{job_id}/approve", json={"confirmation": c}).status_code == 200
    c = confirm(client, job_id, "start")
    assert client.post(f"/api/jobs/{job_id}/start", json={"confirmation": c}).status_code == 428
    c = confirm(client, job_id, "start", text=conn["name"])
    assert client.post(f"/api/jobs/{job_id}/start", json={"confirmation": c}).status_code == 200


def test_pause_is_one_click_and_cancel_works_while_running(client):
    _, job_id = setup_job(client, latency="300")
    client.post(f"/api/jobs/{job_id}/approve", json={"confirmation": confirm(client, job_id, "approve")})
    client.post(f"/api/jobs/{job_id}/start", json={"confirmation": confirm(client, job_id, "start")})
    c = confirm(client, job_id, "cancel")  # stays valid while rows keep completing
    assert client.post(f"/api/jobs/{job_id}/pause", json={}).status_code == 200
    wait_for(lambda: client.get(f"/api/jobs/{job_id}").get_json()["status"] == "paused")
    assert client.post(f"/api/jobs/{job_id}/cancel", json={"confirmation": c}).status_code == 428  # status changed
    c = confirm(client, job_id, "cancel")
    assert client.post(f"/api/jobs/{job_id}/cancel", json={"confirmation": c}).status_code == 200


def test_delete_job_and_connection_need_confirmation(client):
    conn, job_id = setup_job(client)
    assert client.delete(f"/api/jobs/{job_id}", json={}).status_code == 428
    assert client.delete(f"/api/jobs/{job_id}", json={"confirmation": confirm(client, job_id, "delete")}).status_code == 200
    assert client.delete(f"/api/connections/{conn['id']}", json={}).status_code == 428
    s = client.post(f"/api/connections/{conn['id']}/confirm").get_json()
    r = client.delete(f"/api/connections/{conn['id']}", json={"confirmation": {"token": s["token"], "acknowledged": True}})
    assert r.status_code == 200


def test_confirmations_are_audited(client, toolkit):
    _, job_id = setup_job(client)
    client.post(f"/api/jobs/{job_id}/plan", json={"confirmation": confirm(client, job_id, "plan")})
    kinds = [e["kind"] for e in toolkit.store.list_events(job_id)]
    assert "confirmed" in kinds
