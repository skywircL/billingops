"""HTTP contract: identities, idempotency keys, 202s, state conflicts, visibility."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from billingops.api.app import create_app


@pytest.fixture
def client(runtime, billing):
    app = create_app(runtime, start_executor=False)
    with TestClient(app) as c:
        yield c


def hdr(identity: str, key: str | None = None) -> dict[str, str]:
    h = {"X-Demo-Identity": identity}
    h["Idempotency-Key"] = key or uuid.uuid4().hex
    return h


def test_unknown_identity_is_401_and_role_claims_in_the_body_are_ignored(client) -> None:
    assert client.post("/cases", json={"text": "hello there"}, headers={"Idempotency-Key": "k"}).status_code == 401
    r = client.post("/cases", json={"text": "charged twice for INV-2026-0142", "customer_id": "C-1002"}, headers=hdr("cust-c1001"))
    assert r.status_code == 403  # a customer cannot open a case for someone else, whatever the body says


def test_submit_is_202_and_the_idempotency_key_replays_or_conflicts(client, runtime) -> None:
    key = uuid.uuid4().hex
    body = {"text": "I was charged twice for invoice INV-2026-0142, please refund one."}
    first = client.post("/cases", json=body, headers=hdr("cust-c1001", key))
    assert first.status_code == 202 and first.json()["state"] == "investigating"
    again = client.post("/cases", json=body, headers=hdr("cust-c1001", key))
    assert again.status_code == 202 and again.json() == first.json() and again.headers["X-Idempotent-Replay"] == "1"
    other = client.post("/cases", json={"text": "something else entirely"}, headers=hdr("cust-c1001", key))
    assert other.status_code == 409
    assert client.post("/cases", json=body, headers={"X-Demo-Identity": "cust-c1001"}).status_code == 400
    # Same key, different caller: a different scope, so a different operation.
    assert client.post("/cases", json={**body, "customer_id": "C-1001"}, headers=hdr("ops-dana", key)).status_code == 202
    assert len(runtime.store.list_cases()) == 2


def test_full_flow_over_http(client, runtime, billing) -> None:
    r = client.post("/cases", json={"text": "You charged me twice this month, please refund one."}, headers=hdr("cust-c1001"))
    case_id = r.json()["case_id"]
    runtime.drain()
    view = client.get(f"/cases/{case_id}", headers=hdr("cust-c1001")).json()
    assert view["case"]["state"] == "awaiting_user"
    # Another customer cannot see it; an operator can.
    assert client.get(f"/cases/{case_id}", headers=hdr("cust-c1002")).status_code == 404
    assert client.get(f"/cases/{case_id}", headers=hdr("ops-dana")).status_code == 200
    r = client.post(f"/cases/{case_id}/messages", json={"text": "Invoice INV-2026-0142"}, headers=hdr("cust-c1001"))
    assert r.status_code == 202
    runtime.drain()
    view = client.get(f"/cases/{case_id}", headers=hdr("ops-dana")).json()
    assert view["case"]["state"] == "awaiting_approval"
    version = view["case"]["proposal_version"]
    # Customers cannot approve.
    assert client.post(f"/cases/{case_id}/decision", json={"decision": "approve", "proposal_version": version}, headers=hdr("cust-c1001")).status_code == 403
    # Wrong version is a conflict.
    assert client.post(f"/cases/{case_id}/decision", json={"decision": "approve", "proposal_version": version + 1}, headers=hdr("ops-dana")).status_code == 409
    r = client.post(f"/cases/{case_id}/decision", json={"decision": "approve", "proposal_version": version, "note": "ok"}, headers=hdr("ops-dana"))
    assert r.status_code == 202
    runtime.drain()
    view = client.get(f"/cases/{case_id}", headers=hdr("ops-dana")).json()
    assert view["case"]["state"] == "closed" and view["execution"]["status"] == "done"
    events = client.get(f"/cases/{case_id}/events", headers=hdr("ops-dana")).json()
    assert [e["kind"] for e in events if e["kind"] == "refund_verified"] == ["refund_verified"]
    # Closed: further commands conflict, reads keep working.
    assert client.post(f"/cases/{case_id}/messages", json={"text": "again"}, headers=hdr("cust-c1001")).status_code == 409
    assert client.post(f"/cases/{case_id}/escalate", json={"reason": "why not"}, headers=hdr("ops-dana")).status_code == 409
    assert client.get(f"/cases/{case_id}", headers=hdr("cust-c1001")).status_code == 200
    metrics = client.get("/metrics/summary", headers=hdr("ops-dana")).json()
    assert metrics["cases_by_state"]["closed"] == 1 and metrics["events"]["refund_verified"] == 1
    assert metrics["human_wait_seconds_total"] >= 0


def test_escalate_and_resume_are_operator_only_and_state_checked(client, runtime, billing) -> None:
    r = client.post("/cases", json={"text": "charged twice for INV-2026-0142"}, headers=hdr("cust-c1001"))
    case_id = r.json()["case_id"]
    runtime.drain()
    assert client.post(f"/cases/{case_id}/escalate", json={"reason": "take over"}, headers=hdr("cust-c1001")).status_code == 403
    assert client.post(f"/cases/{case_id}/resume", headers=hdr("ops-dana")).status_code == 409  # nothing to resume while awaiting approval
    r = client.post(f"/cases/{case_id}/escalate", json={"reason": "operator takes over"}, headers=hdr("ops-dana"))
    assert r.status_code == 202
    view = client.get(f"/cases/{case_id}", headers=hdr("ops-dana")).json()
    assert view["case"]["state"] == "escalated" and view["case"]["escalation"]["by"] == "ops-dana"
    assert view["case"]["proposal"] is not None  # preserved


def test_index_page_is_served(client) -> None:
    r = client.get("/")
    assert r.status_code == 200 and "BillingOps" in r.text
