"""Write-once execution, unknown outcomes, restarts, and human handoff."""

from __future__ import annotations

import threading
import time

import pytest
from langgraph_production.faults import CrashInjector, SimulatedCrash

import billingops.billing_client as billing_client_module
from billingops.execution.refund import execute_approved

from conftest import approve, run_case


def _one_refund(billing) -> None:
    assert [r["amount_cents"] for r in billing.payment_refunds("C-1001", "PAY-88213")] == [4900]


def test_lost_response_after_commit_is_recovered_by_verification(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    billing.arm_faults(lost_response_after_commit=1)
    view = approve(runtime, case["id"])
    assert view["case"]["state"] == "closed"
    assert view["execution"]["status"] == "done" and view["execution"]["send_result"]["status"] == 500
    assert view["execution"]["sends_recorded"] == 1
    _one_refund(billing)


def test_restart_after_the_write_resumes_by_verifying_not_resending(runtime_factory, billing) -> None:
    """Demo 3: the write succeeds, the process dies before it learns so."""
    rt = runtime_factory(crash_hook=CrashInjector("refund:after_send"))
    case = run_case(rt)["case"]
    rt.submit_decision(case["id"], decision="approve", approver="ops-dana", note="", proposal_version=case["proposal_version"], token=None)
    with pytest.raises(SimulatedCrash):
        rt.drain()
    assert rt.store.pending_tasks(case["id"])[0]["status"] == "running"  # exactly what a dead process leaves behind
    rt.close()

    rt2 = runtime_factory()
    assert rt2.recover_on_startup() == 1
    rt2.drain()
    view = rt2.case_view(case["id"])
    assert view["case"]["state"] == "closed" and view["execution"]["status"] == "done"
    assert view["execution"]["sends_recorded"] == 1
    assert rt2.effects.total_executions() == 1
    _one_refund(billing)
    assert any(e["kind"] == "recovery_verify_first" for e in rt2.store.events(case["id"]))


def test_crash_in_the_send_window_before_the_request_left_never_resends(runtime_factory, billing) -> None:
    """The claim says a send may have happened. Recovery verifies; 404s do not prove otherwise."""
    rt = runtime_factory(crash_hook=CrashInjector("refund:before_send"))
    case = run_case(rt)["case"]
    rt.submit_decision(case["id"], decision="approve", approver="ops-dana", note="", proposal_version=case["proposal_version"], token=None)
    with pytest.raises(SimulatedCrash):
        rt.drain()
    rt.close()

    rt2 = runtime_factory()
    rt2.recover_on_startup()
    rt2.drain()
    view = rt2.case_view(case["id"])
    assert view["case"]["state"] == "uncertain"
    assert view["execution"]["status"] == "in_doubt" and view["execution"]["verify_attempts"] == 3
    assert view["execution"]["last_verification"]["result"] == "not_found"
    assert billing.payment_refunds("C-1001", "PAY-88213") == []
    # An operator may take over, but nobody may declare it not executed or send again.
    rt2.submit_escalation(case["id"], operator="ops-dana", reason="no receipt after three checks")
    rt2.submit_resume(case["id"], actor="ops-dana")
    rt2.drain()
    view = rt2.case_view(case["id"])
    assert view["case"]["state"] == "escalated"
    assert view["execution"]["status"] == "in_doubt" and view["execution"]["verify_attempts"] == 4
    assert rt2.effects.total_executions() == 1  # one send window was ever opened
    assert billing.payment_refunds("C-1001", "PAY-88213") == []


def test_verification_unavailable_keeps_uncertain_and_a_late_success_closes_after_handoff(runtime, billing) -> None:
    """Demos 5 and 6."""
    case = run_case(runtime)["case"]
    billing.arm_faults(verification_unavailable=10)
    view = approve(runtime, case["id"])
    assert view["case"]["state"] == "uncertain"
    assert view["execution"]["status"] == "in_doubt" and view["execution"]["verify_attempts"] == 3
    assert view["execution"]["last_verification"]["result"] == "unavailable"
    _one_refund(billing)  # the money did move; the application does not know yet

    runtime.submit_escalation(case["id"], operator="ops-dana", reason="verification endpoint down")
    view = runtime.case_view(case["id"])
    assert view["case"]["state"] == "escalated"
    assert view["case"]["escalation"]["ledger_status"] == "in_doubt"
    assert view["execution"]["status"] == "in_doubt"  # handoff does not touch the ledger

    runtime.submit_resume(case["id"], actor="ops-dana")  # still down: exactly one query, still unknown
    runtime.drain()
    view = runtime.case_view(case["id"])
    assert view["case"]["state"] == "escalated" and view["execution"]["verify_attempts"] == 4

    billing.clear_faults()
    runtime.submit_resume(case["id"], actor="ops-dana")
    runtime.drain()
    view = runtime.case_view(case["id"])
    assert view["case"]["state"] == "closed" and view["execution"]["status"] == "done"
    assert view["execution"]["receipt"]["refund"]["amount_cents"] == 4900
    assert runtime.effects.total_executions() == 1
    _one_refund(billing)


def test_persisted_terminal_rejection_is_confirmed_not_executed_and_handed_off(runtime, billing) -> None:
    """Demo 7: Billing refuses with a persisted rejection; the ledger records that nothing moved."""
    case = run_case(runtime)["case"]
    proposal = dict(case["proposal"])
    proposal["amount_cents"] = 4901  # one cent over the balance, bypassing precheck on purpose
    runtime.store.update_case(case["id"], state="executing")
    row = execute_approved(runtime.execution, runtime.store.get_case(case["id"]), proposal)
    assert row["status"] == "confirmed_not_executed"
    view = runtime.case_view(case["id"])
    assert view["case"]["state"] == "escalated"
    assert view["case"]["escalation"]["reason"] == "terminal_rejection"
    assert view["case"]["escalation"]["rejection_code"] == "exceeds_refundable_balance"
    assert view["execution"]["sends_recorded"] == 1
    assert billing.payment_refunds("C-1001", "PAY-88213") == []
    # Recovery on a sealed action sends nothing and changes nothing.
    execute_approved(runtime.execution, runtime.store.get_case(case["id"]), proposal)
    assert runtime.effects.total_executions() == 1


def test_same_dispute_through_a_second_action_is_refused_by_billing_not_paid_twice(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    approve(runtime, case["id"])
    _one_refund(billing)
    forged = {**case["proposal"], "action_id": "ffffffffffffffff", "version": 9}
    runtime.store.update_case(case["id"], state="executing")
    row = execute_approved(runtime.execution, runtime.store.get_case(case["id"]), forged)
    assert row["status"] == "confirmed_not_executed"
    assert row["receipt"]["rejection_code"] == "dispute_already_refunded"
    _one_refund(billing)


def test_late_commit_after_repeated_404s_is_found_by_a_later_resume(runtime, billing, monkeypatch) -> None:
    """The controlled concurrency case: the query finds nothing, the original request commits later."""
    monkeypatch.setattr(billing_client_module, "BILLING_WRITE_TIMEOUT_S", 0.3)
    case = run_case(runtime)["case"]
    billing.arm_faults(delay_before_commit_ms=1500, delay_before_commit_uses=1)
    view = approve(runtime, case["id"])
    assert view["case"]["state"] == "uncertain"
    assert view["execution"]["send_result"]["status"] is None  # timed out: unknown
    assert view["execution"]["last_verification"]["result"] == "not_found"
    time.sleep(2.0)
    _one_refund(billing)  # it committed after the checks
    runtime.submit_resume(case["id"], actor="ops-dana")
    runtime.drain()
    view = runtime.case_view(case["id"])
    assert view["case"]["state"] == "closed" and view["execution"]["status"] == "done"
    assert runtime.effects.total_executions() == 1
    _one_refund(billing)


def test_restart_mid_investigation_continues_from_the_checkpoint(runtime_factory, billing) -> None:
    rt = runtime_factory(crash_hook=CrashInjector("run_tool:exit"))
    case = rt.submit_case(customer_id="C-1001", created_by="cust-c1001", text="Charged twice for INV-2026-0142, refund one.")
    with pytest.raises(SimulatedCrash):
        rt.drain()
    rt.close()
    rt2 = runtime_factory()
    rt2.recover_on_startup()
    rt2.drain()
    view = rt2.case_view(case["id"])
    assert view["case"]["state"] == "awaiting_approval"
    assert view["trail"].count("extract") == 1  # extraction was checkpointed, not repeated
    assert view["budgets"]["reads"] == 3


def test_modification_during_execution_is_a_state_conflict_but_reads_still_work(runtime, billing) -> None:
    from billingops.runtime import StateConflict

    case = run_case(runtime)["case"]
    billing.arm_faults(verification_unavailable=10)
    approve(runtime, case["id"])
    runtime.store.set_state(case["id"], "verifying")  # freeze the case mid-verification for the assertion
    with pytest.raises(StateConflict):
        runtime.submit_message(case["id"], text="wait", actor="cust-c1001")
    with pytest.raises(StateConflict):
        runtime.submit_decision(case["id"], decision="approve", approver="ops-dana", note="", proposal_version=1, token=None)
    assert runtime.case_view(case["id"])["case"]["state"] == "verifying"
    assert runtime.store.events(case["id"])
