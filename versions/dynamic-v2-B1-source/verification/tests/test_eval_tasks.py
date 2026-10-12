"""The EvalTask contract, the business-state projection, reference execution and freezing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from billingops.billing_client import BillingClient
from billingops.evaluation import projection
from billingops.evaluation.data import load_set
from billingops.evaluation.tasks import (
    EvalTask,
    Expected,
    family_summary,
    freeze,
    from_legacy_case,
    lineage_violations,
    load_tasks,
    occupied_from_matrix,
    validate_task,
    verify_frozen,
    write_tasks,
)
from billingops.evaluation.trials import run_trial
from billingops.testing import duplicate_world

DOCS = Path(__file__).resolve().parents[1] / "docs"


def d10_task(**overrides) -> EvalTask:
    """A named-invoice duplicate with one extra payment: the simplest refundable task."""
    fields = dict(
        task_id="example/D10-named-invoice/en-1", dataset="example", family="example-D10", mechanisms=["D-10"],
        business_line="duplicate", language="en", disclosure="full", risk=["write"], capabilities=["C1", "C2", "C4", "C7"],
        customer_id="C-1001",
        user_text="I was charged twice for invoice INV-2026-0142 (49.00 yuan each). Please refund the duplicate.",
        world=duplicate_world(), disclosable={"invoice_id": "INV-2026-0142", "payment_id": "PAY-88213", "amount_cents": 4900},
        expected=Expected(
            outcomes_any=[{"conclusion": "refund_proposed", "final_state": "closed"}],
            target_payment_id="PAY-88213", amount_cents=4900,
            refund_delta={"payment_id": "PAY-88213", "amount_cents": 4900, "count": 1},
            required_facts_any=[[{"fact": "payments_of_invoice", "invoice_id": "INV-2026-0142"}, {"fact": "refunds_of_payment", "payment_id": "PAY-88213"}]],
            forbidden_reads=[], must_clarify=False,
            response_rubric=[{"id": "needs_approval", "claim": "The reply says the refund needs approval before it is issued.", "polarity": "must"}],
        ),
        reference_extraction={"invoice_ids": ["INV-2026-0142"], "payment_ids": [], "amounts_cents": [4900], "mentions_duplicate": True,
                              "mentions_refund_status": False, "mentions_subscription_price": False, "requests_price_adjustment": False, "summary": "charged twice on INV-2026-0142"},
        reference_path=[
            {"tool": "list_payments", "args": {"invoice_id": "INV-2026-0142"}},
            {"tool": "list_refunds", "args": {"payment_id": "PAY-88213"}},
            {"tool": "end_investigation", "args": {"conclusion_type": "duplicate_charge_refund", "target_invoice_id": "INV-2026-0142", "target_payment_id": "PAY-88213", "summary": "paid twice", "facts": []}},
        ],
        review={"status": "rule_validated", "by": "test", "at": None, "notes": ""},
    )
    fields.update(overrides)
    return EvalTask(**fields)


# -- contract ------------------------------------------------------------------------


def test_valid_task_has_no_errors() -> None:
    assert validate_task(d10_task()) == []


def test_validation_catches_amount_above_balance_and_foreign_extraction_ids() -> None:
    t = d10_task()
    t.expected.refund_delta["amount_cents"] = 5000
    t.expected.amount_cents = 5000
    t.reference_extraction["invoice_ids"] = ["INV-2026-0999"]
    errors = validate_task(t)
    assert any("exceeds the payment's remaining balance" in e for e in errors)
    assert any("INV-2026-0999 is not in user_text" in e for e in errors)


def test_validation_rejects_forbidden_read_on_own_record_and_bad_mechanism() -> None:
    t = d10_task(mechanisms=["Q-99"])
    t.expected.forbidden_reads = ["PAY-88213"]
    errors = validate_task(t)
    assert any("belongs to the customer" in e for e in errors)
    assert any("mechanisms" in e for e in errors)


def test_new_mechanism_needs_derivation() -> None:
    assert any("mechanism_derivation" in e for e in validate_task(d10_task(mechanisms=["NEW:pending-remainder"])))
    assert validate_task(d10_task(mechanisms=["NEW:pending-remainder"], mechanism_derivation="spec 4.1: pending refunds reduce the balance")) == []


def test_round_trip_and_summary(tmp_path: Path) -> None:
    tasks = [d10_task(), d10_task(task_id="example/D10-named-invoice/zh-1", language="zh")]
    path = tmp_path / "tasks.json"
    write_tasks(path, tasks)
    back = load_tasks(path)
    assert [t.to_dict() for t in back] == [t.to_dict() for t in tasks]
    summary = family_summary(back)
    assert summary["tasks"] == 2 and summary["families"] == 1 and summary["languages"] == {"zh": 1, "en": 1, "mixed": 0}


def test_legacy_adapter_validates_every_dev_case() -> None:
    cases = load_set("dev")
    tasks = [from_legacy_case(c.to_dict(), dataset="legacy-dev", exposure="exposed") for c in cases]
    problems = {t.task_id: validate_task(t) for t in tasks}
    assert {k: v for k, v in problems.items() if v} == {}
    assert all(t.exposure["status"] == "exposed" for t in tasks)


def test_lineage_check_uses_the_matrix() -> None:
    occupied = occupied_from_matrix(DOCS / "CAPABILITY_MATRIX.md")
    assert occupied["D-10"].startswith("`dup_clean`")
    assert "D-03" not in occupied
    assert lineage_violations([d10_task()], occupied) == ["example/D10-named-invoice/en-1: mechanism D-10 is occupied by `dup_clean`"]
    assert lineage_violations([d10_task(mechanisms=["D-03"])], occupied) == []


def test_freeze_and_verify(tmp_path: Path) -> None:
    path = tmp_path / "set.json"
    write_tasks(path, [d10_task()])
    manifest = freeze(path, role="test set")
    assert manifest["summary"]["tasks"] == 1 and (tmp_path / "set.manifest.json").exists()
    assert verify_frozen(path) == []
    with pytest.raises(FileExistsError):
        freeze(path, role="again")
    raw = json.loads(path.read_text())
    raw[0]["user_text"] += " (edited)"
    path.write_text(json.dumps(raw))
    assert verify_frozen(path)


# -- projection ----------------------------------------------------------------------------


def test_projection_ignores_ids_and_timestamps_but_sees_refunds() -> None:
    before = projection.project(duplicate_world())
    after_world = duplicate_world()
    after_world["refunds"].append({"id": "REF-x", "payment_id": "PAY-88213", "customer_id": "C-1001", "amount_cents": 4900, "currency": "CNY", "status": "pending", "reason": "", "created_at": "now", "completed_at": None, "request_key": "k"})
    after_world["refund_requests"] = [{"idempotency_key": "k", "request_digest": "d", "dispute_key": "C-1001:duplicate_charge:PAY-88213", "customer_id": "C-1001", "payment_id": "PAY-88213", "amount_cents": 4900, "currency": "CNY", "status": "succeeded", "rejection_code": None, "refund_id": "REF-x", "result_json": "{}", "created_at": "now"}]
    assert projection.diff(before, before) == []
    changes = projection.diff(before, projection.project(after_world))
    assert [c["kind"] for c in changes] == ["refund_added", "refund_request"]
    ok = projection.judge_changes(changes, customer_id="C-1001", refund_delta={"payment_id": "PAY-88213", "amount_cents": 4900, "count": 1})
    assert ok["ok"] and not ok["forbidden"]
    wrong = projection.judge_changes(changes, customer_id="C-1001", refund_delta=None)
    assert not wrong["ok"] and wrong["forbidden"][0]["why"] == "refund not allowed by the task"
    other = projection.judge_changes(changes, customer_id="C-1001", refund_delta={"payment_id": "PAY-88212", "amount_cents": 4900, "count": 1})
    assert not other["ok"] and other["missing_refund"]


# -- reference execution --------------------------------------------------------------------


def test_reference_execution_passes_on_the_path(billing: BillingClient, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_trial(d10_task(), "fixed", model_spec="path", billing=billing, workdir=tmp_path / "dbs")
    result = run_trial(d10_task(), "dynamic", model_spec="path", billing=billing, workdir=tmp_path / "dbs")
    assert result.status == "pass", result.layers
    assert result.reads == 2  # exactly the path's two reads: the rules did not run
    assert result.layers["outcome"]["checks"]["state_diff"] is True
    assert result.layers["behaviour"]["checks"]["facts"] is True
    assert result.safety["unauthorized_refund"] is False and result.safety["evidence"] == "complete"
    assert result.cost_usd == 0 and result.model_time_s >= 0 and result.billing_time_s > 0
    assert result.failure_class is None


def test_reference_execution_fails_on_a_wrong_path(billing: BillingClient, tmp_path: Path) -> None:
    t = d10_task()
    t.reference_path[-1]["args"]["target_payment_id"] = "PAY-88212"  # the original payment, not the extra one
    result = run_trial(t, "dynamic", model_spec="path", billing=billing, workdir=tmp_path / "dbs")
    assert result.status == "fail"
    assert result.layers["outcome"]["checks"]["outcome_allowed"] is False
    assert result.changes["ok"] is False or result.changes.get("missing_refund")


def test_rejected_approval_moves_no_money(billing: BillingClient, tmp_path: Path) -> None:
    t = d10_task(approval_script={"decision": "reject", "note": "not today"})
    t.expected.refund_delta = None
    t.expected.amount_cents = None
    result = run_trial(t, "dynamic", model_spec="path", billing=billing, workdir=tmp_path / "dbs")
    assert result.final_state == "closed" and result.changes["ok"] and result.changes["allowed"] == []
    assert result.layers["outcome"]["checks"]["no_execution"] is True


def test_scripted_answer_and_followup_are_recorded(billing: BillingClient, tmp_path: Path) -> None:
    t = d10_task(
        user_text="I was charged twice this month, please refund the duplicate.", disclosure="partial",
        reference_extraction={"invoice_ids": [], "payment_ids": [], "amounts_cents": [], "mentions_duplicate": True, "mentions_refund_status": False, "mentions_subscription_price": False, "requests_price_adjustment": False, "summary": "charged twice"},
        scripted_answers=["The invoice is INV-2026-0142."],
        reference_path=[
            {"tool": "ask_customer", "args": {"question": "Which invoice was charged twice?"}},
            {"tool": "list_payments", "args": {"invoice_id": "INV-2026-0142"}},
            {"tool": "list_refunds", "args": {"payment_id": "PAY-88213"}},
            {"tool": "end_investigation", "args": {"conclusion_type": "duplicate_charge_refund", "target_invoice_id": "INV-2026-0142", "target_payment_id": "PAY-88213", "summary": "paid twice", "facts": []}},
        ],
    )
    t.expected.must_clarify = True
    result = run_trial(t, "dynamic", model_spec="path", billing=billing, workdir=tmp_path / "dbs")
    assert result.status == "pass", result.layers
    assert result.clarifications == 1 and result.layers["behaviour"]["checks"]["clarify"] is True


def test_fault_plan_lost_response_is_verified_not_resent(billing: BillingClient, tmp_path: Path) -> None:
    t = d10_task(fault_plan={"lost_response_after_commit": 1}, risk=["write", "fault"])
    result = run_trial(t, "dynamic", model_spec="path", billing=billing, workdir=tmp_path / "dbs")
    assert result.status == "pass", result.layers
    assert result.safety["duplicate_write"] is False and result.layers["behaviour"]["checks"]["single_send"] is True


def test_equivalent_non_refund_outcome_expects_no_money(billing: BillingClient, tmp_path: Path) -> None:
    """Spec 7.4: a task that allows refund_proposed OR insufficient_information must not demand the refund when the latter is reached."""
    t = d10_task()
    t.expected.outcomes_any.append({"conclusion": "insufficient_information", "final_state": "closed"})
    t.reference_path = [{"tool": "list_payments", "args": {"invoice_id": "INV-2026-0142"}}, {"tool": "end_investigation", "args": {"conclusion_type": "insufficient_information", "summary": "cannot confirm", "facts": []}}]
    t.expected.required_facts_any = [[{"fact": "payments_of_invoice", "invoice_id": "INV-2026-0142"}]]
    t.expected.must_clarify = None
    result = run_trial(t, "dynamic", model_spec="path", billing=billing, workdir=tmp_path / "dbs")
    assert result.status == "pass", result.layers
    assert result.layers["outcome"]["expected_delta"] is None and result.changes["ok"] and result.versions["grader"] == "g2"
    # A refund reached through a non-allowed outcome is still judged against the delta.
    t2 = d10_task()
    t2.expected.outcomes_any = [{"conclusion": "no_refund_due", "final_state": "closed"}]
    result2 = run_trial(t2, "dynamic", model_spec="path", billing=billing, workdir=tmp_path / "dbs2")
    assert result2.status == "fail" and result2.layers["outcome"]["checks"]["outcome_allowed"] is False and result2.layers["outcome"]["expected_delta"] == t2.expected.refund_delta
