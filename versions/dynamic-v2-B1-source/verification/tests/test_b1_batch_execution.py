"""Approved multi-item execution over real local Billing; no model API calls."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from langgraph_production.faults import CrashInjector, SimulatedCrash

from billingops.cases.proposal import build_batch_proposal
from billingops.evaluation.v2_scoring import _approval_checks
from billingops.execution.refund import action_key, execute_approved
from billingops.money import RefundableResult


def prepared_batch(rt, billing, *, count=2):
    world = billing.dump_world()
    if count == 2:
        third = deepcopy(next(p for p in world["payments"] if p["id"] == "PAY-88213"))
        third.update(id="PAY-88214", paid_at="2026-02-02T09:02:00")
        world["payments"].append(third)
        billing.load_world(world)
    case = rt.store.create_case(case_id="batch-case", customer_id="C-1001", created_by="cust-c1001", mode="dynamic", text="Refund all excess payments on INV-2026-0142.")
    verdicts = [RefundableResult(True, "refundable", payment_id=pid, invoice_id="INV-2026-0142", amount_cents=4900, currency="CNY") for pid in ["PAY-88213", "PAY-88214"][:count]]
    proposal = build_batch_proposal(case_id=case["id"], customer_id=case["customer_id"], verdicts=verdicts,
                                    evidence_versions={"invoice": "frozen-invoice", "payments": "frozen-payments"}, version=1,
                                    target_resolution={"scope": "invoice", "invoice_ids": ["INV-2026-0142"], "customer_confirmed_all_items": True})
    # Exercise the runtime's actual approval-event serialization contract.
    rt._interpret(case, {"__interrupt__": [SimpleNamespace(value={"kind": "approval", "proposal": proposal, "token": "batch-token"})]})
    rt.store.append_event(case["id"], "approval_recorded", "ops-dana", {"proposal_version": 1, "decision": "approve"})
    rt.store.set_state(case["id"], "executing")
    return rt.store.get_case(case["id"]), proposal


def refunds(billing, pid):
    return billing.payment_refunds("C-1001", pid)


@pytest.mark.parametrize("count", [1, 2])
def test_batch_keeps_approval_and_each_item_key_distinct(runtime, billing, count):
    case, proposal = prepared_batch(runtime, billing, count=count)
    row = execute_approved(runtime.execution, case, proposal)
    view = runtime.case_view(case["id"])
    assert row["status"] == "done" and view["case"]["state"] == "closed"
    assert view["case"]["action_key"] == action_key(case["id"], proposal)
    assert view["execution"]["kind"] == "batch_refund"
    assert view["execution"]["sends_recorded"] == count == runtime.effects.total_executions()
    assert len(view["execution"]["items"]) == count
    for child, item in zip(proposal["items"], view["execution"]["items"]):
        assert item["key"] == action_key(case["id"], child) != view["case"]["action_key"]
        assert item["status"] == "done" and item["sends_recorded"] == 1
        assert item["proposal_version"] == proposal["version"]
        assert [r["amount_cents"] for r in refunds(billing, child["payment_id"])] == [4900]
        assert item["receipt"]["refund"]["id"] in view["case"]["final_message"]
    events = runtime.store.events(case["id"])
    request = next(e["payload"] for e in events if e["kind"] == "approval_requested")
    assert request["items"] == proposal["items"] and request["amount_cents"] == count * 4900
    checks = _approval_checks({"events": events, "view": view})
    assert len(checks) == count and all(c["valid"] and c["proposal_version"] == 1 for c in checks)
    execute_approved(runtime.execution, runtime.store.get_case(case["id"]), proposal)
    assert runtime.effects.total_executions() == count


def test_lost_responses_are_verified_per_item_without_repeating(runtime, billing):
    case, proposal = prepared_batch(runtime, billing)
    billing.arm_faults(lost_response_after_commit=2)
    execute_approved(runtime.execution, case, proposal)
    view = runtime.case_view(case["id"])
    assert view["case"]["state"] == "closed"
    assert [item["send_result"]["status"] for item in view["execution"]["items"]] == [500, 500]
    assert [item["status"] for item in view["execution"]["items"]] == ["done", "done"]
    assert runtime.effects.total_executions() == 2


def test_unknown_first_item_stops_second_and_resume_verifies_then_continues(runtime, billing):
    case, proposal = prepared_batch(runtime, billing)
    billing.arm_faults(verification_unavailable=10)
    execute_approved(runtime.execution, case, proposal)
    view = runtime.case_view(case["id"])
    assert view["case"]["state"] == "uncertain"
    assert [x["status"] for x in view["execution"]["items"]] == ["in_doubt", "not_started"]
    assert view["execution"]["verify_attempts"] == 3
    assert len(refunds(billing, "PAY-88213")) == 1 and refunds(billing, "PAY-88214") == []
    assert runtime.effects.total_executions() == 1
    runtime.submit_escalation(case["id"], operator="ops-dana", reason="waiting for receipt")
    runtime.submit_resume(case["id"], actor="ops-dana")
    runtime.drain()
    assert runtime.case_view(case["id"])["execution"]["verify_attempts"] == 4
    assert runtime.case_view(case["id"])["case"]["state"] == "escalated"
    assert runtime.effects.total_executions() == 1
    billing.clear_faults()
    runtime.submit_resume(case["id"], actor="ops-dana")
    runtime.drain()
    view = runtime.case_view(case["id"])
    assert view["case"]["state"] == "closed" and view["execution"]["status"] == "done"
    assert [len(refunds(billing, pid)) for pid in ("PAY-88213", "PAY-88214")] == [1, 1]
    assert runtime.effects.total_executions() == 2


@pytest.mark.parametrize("crash_point", ["refund:after_send", "batch:after_item"])
def test_restart_resumes_batch_without_resending_finished_or_claimed_items(runtime_factory, billing, monkeypatch, crash_point):
    rt = runtime_factory(crash_hook=CrashInjector(crash_point))
    case, proposal = prepared_batch(rt, billing)
    monkeypatch.setattr(rt, "_start_graph", lambda c: rt._interpret(c, {"outcome": "approved", "proposal": proposal}))
    rt.store.enqueue_task(case["id"], "start", {})
    with pytest.raises(SimulatedCrash):
        rt.drain()
    assert len(refunds(billing, "PAY-88213")) == 1 and refunds(billing, "PAY-88214") == []
    rt.close()
    restarted = runtime_factory()
    assert restarted.recover_on_startup() == 1
    restarted.drain()
    view = restarted.case_view(case["id"])
    assert view["case"]["state"] == "closed" and view["execution"]["status"] == "done"
    assert [item["sends_recorded"] for item in view["execution"]["items"]] == [1, 1]
    assert restarted.effects.total_executions() == 2


def test_crash_before_first_post_never_resends_or_skips_to_next_item(runtime_factory, billing, monkeypatch):
    rt = runtime_factory(crash_hook=CrashInjector("refund:before_send"))
    case, proposal = prepared_batch(rt, billing)
    monkeypatch.setattr(rt, "_start_graph", lambda c: rt._interpret(c, {"outcome": "approved", "proposal": proposal}))
    rt.store.enqueue_task(case["id"], "start", {})
    with pytest.raises(SimulatedCrash):
        rt.drain()
    rt.close()
    restarted = runtime_factory()
    restarted.recover_on_startup()
    restarted.drain()
    view = restarted.case_view(case["id"])
    assert view["case"]["state"] == "uncertain"
    assert [i["status"] for i in view["execution"]["items"]] == ["in_doubt", "not_started"]
    assert restarted.effects.total_executions() == 1
    assert refunds(billing, "PAY-88213") == refunds(billing, "PAY-88214") == []


def external_refund(billing, pid, amount, key):
    return billing.post_refund({"idempotency_key": key, "dispute_key": f"C-1001:external:{key}", "customer_id": "C-1001", "payment_id": pid, "amount_cents": amount, "currency": "CNY", "reason": "external operation during batch test"})


def test_rechecks_whole_invoice_after_first_item_and_does_not_reduce_approved_amount(runtime, billing):
    case, proposal = prepared_batch(runtime, billing)
    fired = False
    def external_after_first(point):
        nonlocal fired
        if point == "batch:after_item" and not fired:
            fired = True
            assert external_refund(billing, "PAY-88212", 4900, "external-original").status == 201
    runtime.execution.crash_hook = external_after_first
    result = execute_approved(runtime.execution, case, proposal)
    view = runtime.case_view(case["id"])
    assert result["status"] == "partial_failure"
    assert result["blocked_reason"]["reason"] == "invoice_balance_changed"
    assert result["blocked_reason"]["invoice_remaining_cents"] == 0
    assert result["blocked_reason"]["approved_remaining_cents"] == 4900
    assert view["case"]["state"] == "escalated"
    assert [i["status"] for i in view["execution"]["items"]] == ["done", "not_started"]
    assert refunds(billing, "PAY-88214") == [] and runtime.effects.total_executions() == 1
    assert view["case"]["proposal"] == proposal
    runtime.submit_resume(case["id"], actor="ops-dana")
    runtime.drain()
    assert runtime.effects.total_executions() == 1


def test_terminal_rejection_after_one_success_is_partial_failure_not_none_executed(runtime, billing, monkeypatch):
    case, proposal = prepared_batch(runtime, billing)
    post = runtime.billing.post_refund
    def concurrent_refund(payload):
        if payload["payment_id"] == "PAY-88214":
            assert external_refund(billing, "PAY-88214", 1, "external-one-cent").status == 201
        return post(payload)
    monkeypatch.setattr(runtime.billing, "post_refund", concurrent_refund)
    result = execute_approved(runtime.execution, case, proposal)
    view = runtime.case_view(case["id"])
    assert result["status"] == "partial_failure" and view["case"]["state"] == "escalated"
    assert [i["status"] for i in view["execution"]["items"]] == ["done", "confirmed_not_executed"]
    assert runtime.effects.total_executions() == 2
    assert [r["amount_cents"] for r in refunds(billing, "PAY-88214")] == [1]
    assert "refund issued" in view["case"]["final_message"] and "confirmed rejection" in view["case"]["final_message"]
    runtime.submit_resume(case["id"], actor="ops-dana")
    runtime.drain()
    assert runtime.effects.total_executions() == 2


def test_transient_precheck_failure_resumes_without_changing_items_or_sealing_false_failure(runtime, billing):
    case, proposal = prepared_batch(runtime, billing)
    billing.arm_faults(reads_unavailable=20)
    result = execute_approved(runtime.execution, case, proposal)
    assert result["status"] == "blocked" and runtime.store.get_case(case["id"])["state"] == "error"
    assert runtime.effects.total_executions() == 0
    billing.clear_faults()
    runtime.submit_resume(case["id"], actor="ops-dana")
    runtime.drain()
    view = runtime.case_view(case["id"])
    assert view["execution"]["status"] == "done" and view["execution"]["blocked_reason"] is None
    assert view["case"]["proposal"] == proposal
    execute_approved(runtime.execution, runtime.store.get_case(case["id"]), proposal)
    assert runtime.store.get_case(case["id"])["state"] == "closed"
    assert runtime.effects.total_executions() == 2


def test_durable_batch_cannot_be_replaced_with_changed_amount_after_a_claim(runtime, billing):
    case, proposal = prepared_batch(runtime, billing)
    billing.arm_faults(verification_unavailable=20)
    execute_approved(runtime.execution, case, proposal)
    changed = deepcopy(proposal)
    changed["items"][1]["amount_cents"] -= 1
    changed["amount_cents"] -= 1
    with pytest.raises(ValueError, match="durable approved execution claim"):
        execute_approved(runtime.execution, runtime.store.get_case(case["id"]), changed)
    assert runtime.effects.total_executions() == 1 and refunds(billing, "PAY-88214") == []


@pytest.mark.parametrize("mutation", ["foreign_customer", "other_invoice", "wrong_version", "wrong_total", "duplicate_payment"])
def test_invalid_batch_envelope_is_rejected_before_any_claim_or_write(runtime, billing, mutation):
    case, proposal = prepared_batch(runtime, billing)
    bad = deepcopy(proposal)
    if mutation == "foreign_customer":
        bad["items"][1]["customer_id"] = "C-2002"
    elif mutation == "other_invoice":
        bad["items"][1]["invoice_id"] = "INV-OTHER"
    elif mutation == "wrong_version":
        bad["items"][1]["version"] += 1
    elif mutation == "wrong_total":
        bad["amount_cents"] += 1
    else:
        bad["items"][1]["payment_id"] = bad["items"][0]["payment_id"]
    with pytest.raises(ValueError):
        execute_approved(runtime.execution, case, bad)
    assert runtime.effects.total_executions() == 0
    assert runtime.effects.get(action_key(case["id"], proposal)) is None
    assert refunds(billing, "PAY-88213") == refunds(billing, "PAY-88214") == []


def test_runtime_passes_explicit_revision_to_graph_deps(runtime):
    assert runtime.deps.agent_revision == runtime.settings.agent_revision == "B0"


def test_lost_response_then_withdrawal_survives_restart_and_only_verifies(runtime_factory, billing):
    rt = runtime_factory()
    case, proposal = prepared_batch(rt, billing)
    billing.arm_faults(lost_response_after_commit=1, verification_unavailable=20)
    execute_approved(rt.execution, case, proposal)
    assert rt.case_view(case["id"])["execution"]["items"][0]["send_result"]["status"] == 500
    command = rt.submit_message(case["id"], text="撤回退款，剩下的不要退了", actor="cust-c1001")
    view = rt.case_view(case["id"])
    assert view["execution"]["status"] == "stopping"
    assert view["execution"]["stop_request"]["command_id"] == command
    assert "Already claimed items can only be verified" in rt.reply_for(case["id"], command)
    rt.close()
    billing.clear_faults()
    restarted = runtime_factory()
    restarted.submit_resume(case["id"], actor="ops-dana")
    restarted.drain()
    view = restarted.case_view(case["id"])
    assert view["case"]["state"] == "escalated" and view["execution"]["status"] == "stopped"
    assert [x["status"] for x in view["execution"]["items"]] == ["done", "not_started"]
    assert "refund issued" in view["case"]["final_message"] and "not sent" in view["case"]["final_message"]
    restarted.submit_message(case["id"], text="确认，继续", actor="cust-c1001")
    restarted.submit_resume(case["id"], actor="ops-dana")
    restarted.drain()
    assert restarted.case_view(case["id"])["execution"]["stop_request"]["command_id"] == command
    assert restarted.effects.total_executions() == 1 and refunds(billing, "PAY-88214") == []
    assert restarted.store.get_case(case["id"])["proposal"] == proposal


@pytest.mark.parametrize("message, expected_kind, sends", [
    ("取消退款", "withdraw", 1),
    ("我刚才报错了，是另一张账单 INV-2026-9999", "new_info", 1),
    ("先确认一下我的新要求", "unclear", 1),
    ("现在处理到哪一步了？", "progress", 2),
    ("Any update?", "progress", 2),
    ("Any update? Also use a different invoice.", "new_info", 1),
])
def test_customer_message_between_items_cannot_be_hidden_behind_task_queue(runtime, billing, message, expected_kind, sends):
    case, proposal = prepared_batch(runtime, billing)
    commands = []
    def message_after_first(point):
        if point == "batch:after_item" and not commands:
            commands.append(runtime.submit_message(case["id"], text=message, actor="cust-c1001"))
    runtime.execution.crash_hook = message_after_first
    execute_approved(runtime.execution, case, proposal)
    view = runtime.case_view(case["id"])
    observations = [e for e in runtime.store.events(case["id"]) if e["kind"] == "batch_followup_classified"]
    assert len(observations) == 1 and observations[0]["payload"]["kind"] == expected_kind
    assert runtime.effects.total_executions() == sends
    assert runtime.store.pending_tasks(case["id"]) == []
    if sends == 1:
        assert view["execution"]["status"] == "stopped" and view["case"]["state"] == "escalated"
        assert refunds(billing, "PAY-88214") == []
        assert runtime.reply_for(case["id"], commands[0])
    else:
        assert view["execution"]["status"] == "done" and view["case"]["state"] == "closed"


def test_command_accepted_during_second_precheck_is_seen_before_new_claim(runtime, billing, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    case, proposal = prepared_batch(runtime, billing)
    entered, release = Event(), Event()
    get_invoice = runtime.billing.get_invoice
    reads = 0
    def blocked_second_read(customer, invoice):
        nonlocal reads
        reads += 1
        result = get_invoice(customer, invoice)
        if reads == 2:
            entered.set()
            assert release.wait(5), "test did not release the Billing read"
        return result
    monkeypatch.setattr(runtime.billing, "get_invoice", blocked_second_read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_approved, runtime.execution, case, proposal)
        assert entered.wait(5)
        try:
            runtime.submit_message(case["id"], text="Do not refund the remaining payment", actor="cust-c1001")
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result["status"] == "stopped"
    assert runtime.effects.total_executions() == 1 and refunds(billing, "PAY-88214") == []
    assert runtime.effects.get(action_key(case["id"], proposal["items"][1])) is None


def test_durable_command_without_event_or_task_survives_execution_crash(runtime_factory, billing, monkeypatch):
    rt = runtime_factory(crash_hook=CrashInjector("refund:after_send"))
    case, proposal = prepared_batch(rt, billing)
    monkeypatch.setattr(rt, "_start_graph", lambda c: rt._interpret(c, {"outcome": "approved", "proposal": proposal}))
    rt.store.enqueue_task(case["id"], "start", {})
    with pytest.raises(SimulatedCrash):
        rt.drain()
    # Simulate dying after message persistence, before reply/event/queue writes.
    command = rt.store.record_command(case_id=case["id"], kind="message", payload={"text": "Cancel the remaining refunds"}, actor="cust-c1001")
    rt.close()
    restarted = runtime_factory()
    restarted.recover_on_startup()
    restarted.drain()
    view = restarted.case_view(case["id"])
    assert view["execution"]["status"] == "stopped"
    assert view["execution"]["stop_request"]["command_id"] == command
    assert [x["status"] for x in view["execution"]["items"]] == ["done", "not_started"]
    assert restarted.effects.total_executions() == 1 and refunds(billing, "PAY-88214") == []


def test_command_after_approval_before_root_claim_stops_first_send(runtime, billing):
    case, proposal = prepared_batch(runtime, billing)
    runtime.store.record_command(case_id=case["id"], kind="decision", payload={"decision": "approve", "proposal_version": proposal["version"]}, actor="ops-dana")
    command = runtime.submit_message(case["id"], text="取消退款", actor="cust-c1001")
    result = execute_approved(runtime.execution, case, proposal)
    assert result["status"] == "stopped" and result["stop_request"]["command_id"] == command
    assert runtime.effects.total_executions() == 0
    assert refunds(billing, "PAY-88213") == refunds(billing, "PAY-88214") == []


def test_preapproval_confirmation_is_not_reinterpreted_as_new_information(runtime, billing):
    case, proposal = prepared_batch(runtime, billing)
    runtime.store.record_command(case_id=case["id"], kind="message", payload={"text": "确认两笔49元，合计98元"}, actor="cust-c1001")
    approved = runtime.store.record_command(case_id=case["id"], kind="decision", payload={"decision": "approve", "proposal_version": proposal["version"]}, actor="ops-dana")
    runtime.store.append_event(case["id"], "approval_recorded", "ops-dana", {"decision": "approve", "proposal_version": proposal["version"], "command_id": approved})
    result = execute_approved(runtime.execution, case, proposal)
    assert result["status"] == "done" and result.get("stop_request") is None
    assert runtime.effects.total_executions() == 2


def test_later_unconsumed_duplicate_approval_cannot_hide_customer_withdrawal(runtime, billing):
    case, proposal = prepared_batch(runtime, billing)
    approved = runtime.store.record_command(case_id=case["id"], kind="decision", payload={"decision": "approve", "proposal_version": proposal["version"]}, actor="ops-dana")
    runtime.store.append_event(case["id"], "approval_recorded", "ops-dana", {"decision": "approve", "proposal_version": proposal["version"], "command_id": approved})
    withdrawal = runtime.store.record_command(case_id=case["id"], kind="message", payload={"text": "Cancel the refund"}, actor="cust-c1001")
    runtime.store.record_command(case_id=case["id"], kind="decision", payload={"decision": "approve", "proposal_version": proposal["version"]}, actor="ops-dana")
    result = execute_approved(runtime.execution, case, proposal)
    assert result["status"] == "stopped" and result["stop_request"]["command_id"] == withdrawal
    assert runtime.effects.total_executions() == 0


def test_already_escalated_case_refreshes_receipts_when_later_scope_check_fails(runtime, billing):
    case, proposal = prepared_batch(runtime, billing)
    billing.arm_faults(verification_unavailable=20)
    execute_approved(runtime.execution, case, proposal)
    runtime.submit_escalation(case["id"], operator="ops-dana", reason="verify the unknown first item")
    assert external_refund(billing, "PAY-88212", 4900, "external-original-after-escalation").status == 201
    billing.clear_faults()
    runtime.submit_resume(case["id"], actor="ops-dana")
    runtime.drain()
    view = runtime.case_view(case["id"])
    assert view["execution"]["status"] == "partial_failure" and view["case"]["state"] == "escalated"
    assert "refund issued" in view["case"]["final_message"] and "not sent" in view["case"]["final_message"]
    assert "still being verified" not in view["case"]["final_message"]
    assert runtime.effects.total_executions() == 1
