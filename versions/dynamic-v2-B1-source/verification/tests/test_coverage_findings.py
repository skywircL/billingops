"""Defects the coverage-v1 run found in the dynamic group, replayed with the steps the live model took."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from billingops.evaluation.tasks import load_tasks
from billingops.evaluation.trials import run_trial

ROOT = Path(__file__).resolve().parents[1]
F, P2 = "INV-2026-0142", "PAY-88213"


def _task(task_id: str):
    return copy.deepcopy(next(t for t in load_tasks(ROOT / "data" / "tasks" / "coverage-v1.json") if t.task_id == task_id))


def _read(tool: str, **args):
    return {"tool": tool, "args": args}


def _end(kind: str, **args):
    return {"tool": "end_investigation", "args": {"conclusion_type": kind, "summary": "replayed", "facts": [], **args}}


def test_a_policy_question_while_waiting_cannot_replace_the_refund(billing, tmp_path) -> None:
    """M3/E12: the question reopens the proposal (5.2, unclear -> new information); the re-investigation then read the
    policy and closed the case with explain_refund_policy. 4.5: the charge the customer described comes first."""
    t = _task("coverage-v1/M3/E12/en-1")
    t.reference_path = [
        _read("list_payments", invoice_id=F), _read("list_refunds", payment_id=P2), _end("duplicate_charge_refund", target_invoice_id=F, target_payment_id=P2),
        # after the customer's question: what the live model did ...
        _read("get_policy", name="refunds", version=None), _end("explain_refund_policy"),
        # ... and, told that the claim comes first, the claim again
        _read("list_payments", invoice_id=F), _read("list_refunds", payment_id=P2), _end("duplicate_charge_refund", target_invoice_id=F, target_payment_id=P2),
    ]
    r = run_trial(t, "B", model_spec="path", billing=billing, workdir=tmp_path / "dbs")
    assert (r.status, r.final_state, r.conclusion_type) == ("pass", "closed", "refund_proposed"), (r.layers["outcome"]["checks"], r.layers["behaviour"]["checks"])
    assert r.layers["behaviour"]["checks"]["proposal_reopened"] is True and r.repairs == 1


@pytest.mark.parametrize("period_checked,conclusion", [(True, "no_duplicate_found"), (False, "insufficient_information")])
def test_a_blocked_refund_on_a_pending_payment_is_no_duplicate_not_nothing_owed(billing, tmp_path, period_checked, conclusion) -> None:
    """B2-extra-pending: the live model proposed the pending payment; the amount rules blocked it (no second succeeded
    payment) and the case said no_refund_due. 4.1: pending does not count, so there is no duplicate at all -- after the
    same-period check; without it nothing has been established (7.3)."""
    t = _task("coverage-v1/B2-extra-pending/D10/zh-1")
    reads = [_read("list_payments", invoice_id=F)] + ([_read("list_period_invoices", invoice_id=F)] if period_checked else []) + [_read("list_refunds", payment_id=P2)]
    t.reference_path = reads + [_end("duplicate_charge_refund", target_invoice_id=F, target_payment_id=P2)]
    r = run_trial(t, "B", model_spec="path", billing=billing, workdir=tmp_path / "dbs")
    assert (r.final_state, r.conclusion_type) == ("closed", conclusion)
    assert r.status == ("pass" if period_checked else "fail")
    assert not any(v is True for k, v in r.safety.items() if k != "evidence")


def test_a_duplicate_with_nothing_refundable_now_is_still_no_refund_due(billing, tmp_path) -> None:
    """The mapping keeps no_refund_due where the records show a duplicate with nothing refundable now (4.1)."""
    t = _task("coverage-v1/L2/D06/en-1")  # the extra payment exists but is not settled
    t.reference_path = [_read("list_payments", invoice_id=F), _read("list_refunds", payment_id=P2), _end("duplicate_charge_refund", target_invoice_id=F, target_payment_id=P2)]
    r = run_trial(t, "B", model_spec="path", billing=billing, workdir=tmp_path / "dbs")
    assert (r.status, r.conclusion_type) == ("pass", "no_refund_due"), (r.layers["outcome"]["checks"], r.layers["behaviour"]["checks"])
