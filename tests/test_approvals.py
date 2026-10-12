"""Approval binding: missing, forged, expired, reused, and out-of-date approvals produce no refund."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from langgraph.types import Command

from billingops.runtime import StateConflict

from conftest import approve, run_case

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc).isoformat()


def _resume_raw(rt, case_id: str, payload) -> None:
    """Bypass the API and hand the gate real rubbish, as a compromised caller would."""
    rt.graph.invoke(Command(resume=payload), rt._config(case_id), durability="sync")


def _decision(case, **over):
    base = {"type": "decision", "command_id": "cmd-forged", "token": case["gate_token"], "approver": "ops-dana", "decision": "approve", "note": "", "decided_at": NOW, "proposal_version": case["proposal_version"]}
    base.update(over)
    return base


def test_forged_token_is_rejected_and_the_case_keeps_waiting(runtime, billing) -> None:
    view = run_case(runtime)
    case = view["case"]
    _resume_raw(runtime, case["id"], _decision(case, token="0" * 32))
    snapshot = runtime._snapshot(case["id"])
    assert snapshot["gate_rejections"] == ["token_mismatch"] and snapshot.get("approval") is None
    assert runtime._graph_is_interrupted(case["id"])  # still at the gate
    assert billing.payment_refunds("C-1001", "PAY-88213") == []


def test_malformed_and_missing_approval_fields_are_rejected(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    _resume_raw(runtime, case["id"], {"type": "decision", "command_id": "c1"})
    _resume_raw(runtime, case["id"], "approve")
    snapshot = runtime._snapshot(case["id"])
    assert snapshot["gate_rejections"] == ["malformed_decision", "malformed_decision"]
    assert billing.payment_refunds("C-1001", "PAY-88213") == []


def test_approval_for_a_stale_proposal_version_is_rejected(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    _resume_raw(runtime, case["id"], _decision(case, proposal_version=case["proposal_version"] + 1))
    assert runtime._snapshot(case["id"])["gate_rejections"] == ["proposal_version_mismatch"]
    with pytest.raises(StateConflict):
        runtime.submit_decision(case["id"], decision="approve", approver="ops-dana", note="", proposal_version=99, token=None)


def test_token_reuse_for_a_different_decision_is_refused_by_the_ledger(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    # First a denial, bound in the ledger to this token.
    _resume_raw(runtime, case["id"], _decision(case, decision="reject", command_id="c-deny", note="not entitled"))
    assert runtime.store.get_case(case["id"])["state"] == "awaiting_approval"  # the runtime has not been told yet
    # The graph ended with outcome rejected; a replay of the same token with a different verdict must not authorise.
    from langgraph_production.approvals import AuthorizationError, authorize
    from langgraph_production.models import ApprovalRecord

    record = ApprovalRecord(token=case["gate_token"], ticket_id=case["id"], action_id=case["proposal"]["action_id"], kind="approval", approver="ops-dana", decision="approve", note="", decided_at=datetime.fromisoformat(NOW), state_digest=case["proposal"]["fingerprint"])
    with pytest.raises(AuthorizationError) as excinfo:
        authorize(record=record, expected_token=case["gate_token"], ledger=runtime.approvals)
    assert excinfo.value.reason == "token_replayed"
    assert runtime.approvals.count() == 1
    assert billing.payment_refunds("C-1001", "PAY-88213") == []


def test_rejection_closes_without_a_refund(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    runtime.submit_decision(case["id"], decision="reject", approver="ops-dana", note="customer already compensated", proposal_version=case["proposal_version"], token=None)
    runtime.drain()
    view = runtime.case_view(case["id"])
    assert view["case"]["state"] == "closed" and view["execution"] is None
    assert "not approved" in view["case"]["final_message"]
    assert billing.payment_refunds("C-1001", "PAY-88213") == []


@pytest.mark.parametrize("mode", ["dynamic", "fixed"])
def test_proposal_modification_invalidates_the_old_approval(runtime_factory, billing, mode: str) -> None:
    """Demo 4: the customer adds information while a proposal waits; the old token must die."""
    rt = runtime_factory(mode=mode)
    case = run_case(rt)["case"]
    old_token, old_version = case["gate_token"], case["proposal_version"]
    rt.submit_message(case["id"], text="Actually the second payment was PAY-88213, please double check.", actor="cust-c1001")
    rt.drain()
    view = rt.case_view(case["id"])
    assert view["case"]["state"] == "awaiting_approval"
    assert view["case"]["proposal_version"] == old_version + 1
    assert view["invalidated_proposals"][0]["version"] == old_version
    # An approval carrying the old version is refused at the API...
    with pytest.raises(StateConflict):
        rt.submit_decision(case["id"], decision="approve", approver="ops-dana", note="", proposal_version=old_version, token=old_token)
    # ...and a raw approval carrying the old token never reaches execution.
    _resume_raw(rt, case["id"], _decision(view["case"], token=old_token, proposal_version=view["case"]["proposal_version"]))
    assert rt._snapshot(case["id"])["gate_rejections"][-1] == "token_mismatch"
    assert billing.payment_refunds("C-1001", "PAY-88213") == []
    # The new proposal approves normally, once.
    view = approve(rt, case["id"])
    assert view["case"]["state"] == "closed"
    assert len(billing.payment_refunds("C-1001", "PAY-88213")) == 1


def test_race_between_modification_and_approval_produces_one_effective_command(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    # Both commands are persisted before either runs. The message goes first and invalidates the proposal.
    runtime.submit_message(case["id"], text="One more thing: it was on the February invoice.", actor="cust-c1001")
    decision_payload = {"command_id": "cmd-late-approval", "token": case["gate_token"], "approver": "ops-dana", "decision": "approve", "note": "", "decided_at": NOW, "proposal_version": case["proposal_version"]}
    runtime.store.enqueue_task(case["id"], "decision", decision_payload)
    runtime.drain()
    view = runtime.case_view(case["id"])
    assert view["case"]["state"] == "awaiting_approval" and view["case"]["proposal_version"] == 2
    assert "token_mismatch" in runtime._snapshot(case["id"])["gate_rejections"] or "proposal_version_mismatch" in runtime._snapshot(case["id"])["gate_rejections"]
    assert billing.payment_refunds("C-1001", "PAY-88213") == []


def test_duplicate_command_is_consumed_once(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    command_id = runtime.submit_decision(case["id"], decision="approve", approver="ops-dana", note="", proposal_version=case["proposal_version"], token=None)
    payload = runtime.store.tasks(case["id"])[-1]
    import json

    runtime.store.enqueue_task(case["id"], "decision", json.loads(payload["payload_json"]))  # the same command, queued twice
    runtime.drain()
    events = [e["kind"] for e in runtime.store.events(case["id"])]
    assert events.count("approval_recorded") == 1
    assert "duplicate_command_ignored" in events or "command_not_applicable" in events
    assert len(billing.payment_refunds("C-1001", "PAY-88213")) == 1
    assert runtime.effects.total_executions() == 1


def test_conditions_changed_between_approval_and_execution_reproposes(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    # Someone else refunds part of the payment after the proposal was made.
    world = billing.dump_world()
    world["refunds"].append({"id": "RF-EXT", "payment_id": "PAY-88213", "customer_id": "C-1001", "amount_cents": 1000, "currency": "CNY", "status": "completed", "reason": "external", "created_at": "2026-03-01T00:00:00", "completed_at": "2026-03-01T00:00:00", "request_key": None})
    billing.load_world(world)
    view = approve(runtime, case["id"])
    assert view["case"]["state"] == "awaiting_approval"
    assert view["case"]["proposal_version"] == 2 and view["case"]["proposal"]["amount_cents"] == 3900
    assert view["execution"] is None  # nothing was sent under the old approval
    assert any(e["kind"] == "conditions_changed" for e in runtime.store.events(case["id"]))
    view = approve(runtime, case["id"])
    assert view["case"]["state"] == "closed"
    assert sum(r["amount_cents"] for r in billing.payment_refunds("C-1001", "PAY-88213")) == 4900


def test_transcript_stays_well_formed_after_a_modification(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    runtime.submit_message(case["id"], text="Also check PAY-88213 please.", actor="cust-c1001")
    runtime.drain()
    messages = runtime._snapshot(case["id"])["messages"]
    open_calls: set[str] = set()
    for m in messages:
        if m["role"] == "assistant":
            open_calls |= {tc["id"] for tc in m.get("tool_calls", [])}
        elif m["role"] == "tool":
            open_calls.discard(m["tool_call_id"])
        elif m["role"] == "user":
            assert not open_calls, "a user turn was appended while a tool call was unanswered"
    assert runtime.store.get_case(case["id"])["state"] == "awaiting_approval"
