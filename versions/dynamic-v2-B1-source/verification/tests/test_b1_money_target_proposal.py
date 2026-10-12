"""B1 invoice semantics; local rule tests, not estimates of model quality."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from billingops.cases.proposal import build_batch_proposal, same_batch_conditions
from billingops.investigation.evidence import build_index
from billingops.investigation.target import resolve_invoice_target, resolve_target
from billingops.money import evaluate_duplicate_refund, evaluate_invoice_refund


def records(count=2, amount=4900):
    invoice = {"id": "INV-1", "customer_id": "C-1", "amount_cents": amount,
               "currency": "CNY", "period_start": "2026-10-01", "subscription_id": "SUB-1"}
    payments = [{"id": f"PAY-{i}", "invoice_id": "INV-1", "customer_id": "C-1",
                 "amount_cents": amount, "currency": "CNY", "status": "succeeded",
                 "settled": 1, "paid_at": f"2026-10-02T09:00:0{i}+08:00"}
                for i in range(1, count + 1)]
    return invoice, payments, {p["id"]: [] for p in payments}


def money(count=2, amount=4900, **overrides):
    inv, payments, refunds = records(count, amount)
    return evaluate_invoice_refund(**{**dict(customer_id="C-1", invoice=inv,
                                            payments=payments, refunds_by_payment=refunds), **overrides})


def refund(pid, amount=4900, status="completed"):
    return {"id": f"REF-{pid}", "payment_id": pid, "customer_id": "C-1",
            "currency": "CNY", "amount_cents": amount, "status": status}


def index(count=2):
    inv, payments, refunds = records(count)
    return build_index([
        {"tool": "get_invoice", "ok": True, "data": inv},
        {"tool": "list_payments", "args": {"invoice_id": "INV-1"}, "ok": True, "data": payments},
        *[{"tool": "list_refunds", "args": {"payment_id": pid}, "ok": True, "data": rows}
          for pid, rows in refunds.items()],
    ])


def state(text="请退清账单INV-1多扣的钱。", role="unspecified", action="refund", **extracted):
    return {"customer_id": "C-1", "text": text,
            "extracted": {"request_action": action, "payment_reference_role": role, **extracted}}


@pytest.mark.parametrize("amount", [1, 4900, 10000])
@pytest.mark.parametrize("count", [2, 3])
def test_whole_invoice_allocates_every_excess_payment(count, amount):
    result = money(count=count, amount=amount)
    assert result.eligible
    assert result.amount_cents == (count - 1) * amount
    assert result.retained_payment_id == "PAY-1"
    assert [v.payment_id for v in result.items] == [f"PAY-{i}" for i in range(2, count + 1)]
    assert all(v.amount_cents == amount for v in result.items)


def test_even_retained_payment_needs_complete_refund_evidence():
    result = money(refunds_by_payment={"PAY-2": []})
    assert not result.eligible and result.reason == "invoice_refunds_not_read"
    assert result.missing_refund_payment_ids == ("PAY-1",)


@pytest.mark.parametrize("status", ["pending", "completed"])
def test_refund_on_original_payment_consumes_whole_invoice_allowance(status):
    result = money(refunds_by_payment={"PAY-1": [refund("PAY-1", status=status)], "PAY-2": []})
    assert not result.eligible and result.reason == "already_fully_refunded"
    assert result.already_refunded_cents == 4900 and result.available_cents == 0
    result = money(count=3, refunds_by_payment={"PAY-1": [refund("PAY-1", status=status)], "PAY-2": [], "PAY-3": []})
    assert result.eligible and result.retained_payment_id == "PAY-2"
    assert [v.payment_id for v in result.items] == ["PAY-3"]


def test_customer_can_explicitly_refund_original_without_overrefund():
    result = money(requested_payment_ids=["PAY-1"])
    assert result.eligible and result.retained_payment_id == "PAY-2"
    assert [v.payment_id for v in result.items] == ["PAY-1"]
    assert money(requested_payment_ids=["PAY-1", "PAY-2"]).reason == "requested_scope_exceeds_invoice_budget"
    assert money(requested_payment_ids=["PAY-1", "PAY-1"]).reason == "invalid_explicit_payment_scope"


def test_legacy_original_and_partial_rules_are_unchanged():
    inv, payments, _ = records()
    args = dict(customer_id="C-1", invoice=inv, payments=payments,
                refunds_by_payment={"PAY-2": [refund("PAY-2", amount=1700)]})
    assert evaluate_duplicate_refund(**args, target_payment_id="PAY-1").reason == "target_is_not_an_extra_payment"
    assert evaluate_duplicate_refund(**args, target_payment_id="PAY-2").amount_cents == 3200
    assert money(refunds_by_payment={"PAY-1": [], "PAY-2": [refund("PAY-2", amount=1700)]}).reason == "unsupported_historical_partial_refund"


@pytest.mark.parametrize("change,reason", [
    ({"customer_id": "C-2"}, "payment_not_owned_by_customer"),
    ({"invoice_id": "INV-2"}, "payment_invoice_mismatch"),
    ({"currency": "USD"}, "payment_currency_mismatch"),
    ({"amount_cents": -1}, "invalid_payment_amount"),
    ({"amount_cents": 4901}, "unsupported_non_full_payment"),
    ({"settled": 0}, "payment_not_settled"),
])
def test_invalid_payment_envelope_stops_new_actions(change, reason):
    _, payments, _ = records()
    payments[0].update(change)  # Also validate the supposedly retained payment.
    assert money(payments=payments).reason == reason


def test_no_clamping_of_refund_anomalies_to_normal_already_refunded():
    assert money(refunds_by_payment={"PAY-1": [refund("PAY-1")], "PAY-2": [refund("PAY-2")]}).reason == "historical_refunds_exceed_invoice_overcharge"
    assert money(refunds_by_payment={"PAY-1": [], "PAY-2": [refund("PAY-2", amount=5000)]}).reason == "historical_refunds_exceed_payment"
    assert money(refunds_by_payment={"PAY-1": [], "PAY-2": [refund("PAY-OTHER")]}).reason == "refund_payment_mismatch"
    assert money(refunds_by_payment={"PAY-1": [], "PAY-2": [refund("PAY-2", status="unknown")]}).reason == "unknown_refund_status"


def test_unresolved_reservations_and_ledger_rows_do_not_free_capacity():
    assert money(reserved_by_payment={"PAY-2": 4900}).reason == "refund_in_doubt"
    result = money(count=3, reserved_by_payment={"PAY-2": 4900})
    assert result.eligible and [v.payment_id for v in result.items] == ["PAY-3"]
    assert money(reserved_by_payment={"PAY-2": 5000}).reason == "refund_and_reservation_exceed_payment"
    assert money(reserved_by_payment={"PAY-OTHER": 1}).reason == "invalid_unresolved_reservation"
    # The caller must exclude a reservation represented by a ledger row;
    # double occupancy is rejected, never treated as newly available money.
    assert money(refunds_by_payment={"PAY-1": [], "PAY-2": [refund("PAY-2")]}, reserved_by_payment={"PAY-2": 4900}).reason == "refund_and_reservation_exceed_payment"
    assert money(refunds_by_payment={"PAY-1": [], "PAY-2": [refund("PAY-2")]}, reserved_by_payment={"PAY-1": 0}).reason == "already_fully_refunded"


def test_pending_and_failed_payments_are_not_duplicate_successes():
    _, payments, _ = records()
    for status in ["pending", "failed"]:
        payments[1]["status"] = status
        assert money(payments=payments).reason == "no_duplicate_payment"


def test_invoice_amount_must_be_read_not_inferred_from_payments():
    inv, _, _ = records()
    del inv["amount_cents"]
    assert money(invoice=inv).reason == "invoice_amount_not_read"


def test_locator_does_not_restrict_to_original_payment_but_exclusive_does():
    idx = index()
    locating = state("我只有付款凭证PAY-1，帮我退回这张账单多扣的钱。", role="locator")
    resolution = resolve_invoice_target(locating, idx, "INV-1", ["PAY-2"])
    assert resolution["ok"] and resolution["scope"] == "invoice"
    assert resolution["requested_payment_ids"] == []
    exclusive = state("只退PAY-1，其他付款不要动。", role="locator")
    assert resolve_invoice_target(exclusive, idx, "INV-1", ["PAY-2"])["reason"] == "customer_payment_mismatch"
    accepted = resolve_invoice_target(exclusive, idx, "INV-1", ["PAY-1"])
    assert accepted["ok"] and accepted["requested_payment_ids"] == ["PAY-1"]
    assert accepted["payment_reference_role"] == "exclusive"
    # Legacy execution still treats any named payment as the target.
    assert resolve_target(locating, idx, "INV-1", "PAY-2")["reason"] == "customer_payment_mismatch"


@pytest.mark.parametrize("text", [
    "先查清INV-1，退款先别办。", "账单INV-1只核查，先不要退款。",
    "INV-1: please investigate; hold off on the refund for now.",
])
def test_inquiry_is_resolved_without_granting_financial_authorization(text):
    resolution = resolve_invoice_target(state(text, action="refund"), index(), "INV-1")
    assert resolution["ok"] and resolution["request_action"] == "inquire"
    assert resolution["authorization"] == "refund_not_requested"


def test_semantic_request_is_not_rejected_by_a_refund_keyword_allowlist():
    assert resolve_invoice_target(state("请调查并处理INV-1的重复扣费。"), index(), "INV-1")["ok"]


def test_two_extras_are_one_invoice_scope_not_target_ambiguity():
    resolution = resolve_invoice_target(state(), index(3), "INV-1", ["PAY-2"])
    assert resolution["ok"] and resolution["invoice_ids"] == ["INV-1"]
    assert resolution["requested_payment_ids"] == []


def test_payment_references_need_billing_evidence_and_cannot_supply_arbitrary_targets():
    idx = index()
    assert resolve_invoice_target(state("退PAY-UNKNOWN", role="locator"), idx, "INV-1")["reason"] == "payment_reference_not_read"
    assert resolve_invoice_target(state("退PAY-1"), idx, "INV-1")["reason"] == "payment_reference_role_unresolved"
    assert resolve_invoice_target(state("退PAY-1", role="locator"), idx, "INV-2")["reason"] == "customer_invoice_mismatch"
    idx.payments["PAY-1"]["customer_id"] = "C-OTHER"
    assert resolve_invoice_target(state("退PAY-1", role="locator"), idx, "INV-1")["reason"] == "payment_not_owned_by_customer"


def test_actual_customer_correction_is_preserved_and_model_cannot_replace_bad_id():
    s = state("请处理账单INV-BAD的多扣款。")
    idx = index()
    assert resolve_invoice_target(s, idx, "INV-1")["reason"] == "customer_invoice_mismatch"
    s["clarifications"] = [{"answer": "刚才错了，是INV-1。", "command_id": "CMD-7"}]
    resolution = resolve_invoice_target(s, idx, "INV-1")
    assert resolution["ok"] and resolution["source_utterance"]["command_id"] == "CMD-7"
    assert resolution["source_command_ids"] == ["CMD-7"]


def test_conflicting_ids_require_failed_lookup_or_actual_correction():
    s = state("退款INV-BAD，付款凭证PAY-1。", role="locator")
    assert resolve_invoice_target(s, index(), "INV-1")["reason"] == "conflicting_customer_identifiers"
    s["evidence"] = [{"tool": "get_invoice", "args": {"invoice_id": "INV-BAD"}, "ok": False, "error": "not_found:INV-BAD"}]
    assert resolve_invoice_target(s, index(), "INV-1")["ok"]


def scope_index():
    idx = index(3)
    idx.invoice_list = [idx.invoices["INV-1"],
                        {**idx.invoices["INV-1"], "id": "INV-OLD", "period_start": "2026-09-01"},
                        {**idx.invoices["INV-1"], "id": "INV-SEAT", "subscription_id": "SUB-2"}]
    for inv in idx.invoice_list:
        idx.invoices[inv["id"]] = inv
        if inv["id"] != "INV-1":
            idx.payments_by_invoice[inv["id"]] = [{**p, "id": f"{p['id']}-{inv['id']}", "invoice_id": inv["id"]}
                                                for p in idx.payments_by_invoice["INV-1"][:2]]
    idx.subscription_history = {"subscriptions": [{"id": "SUB-1", "customer_id": "C-1", "plan": "Pro"},
                                                  {"id": "SUB-2", "customer_id": "C-1", "plan": "Seats"}]}
    return idx


def test_month_plan_scope_filters_actual_invoice_and_subscription_evidence():
    s = state("本月Pro套餐多扣了，请处理。", scope_month="2026-10", scope_plan="Pro")
    idx = scope_index()
    resolution = resolve_invoice_target(s, idx, "INV-1", as_of="2026-10-08T00:00:00+08:00")
    assert resolution["ok"] and resolution["scope"] == "month_plan"
    assert resolution["candidate_invoice_ids"] == ["INV-1"]
    assert resolve_invoice_target(s, idx, "INV-1")["reason"] == "scope_month_not_supported_by_customer"
    idx.subscription_history = None
    assert resolve_invoice_target(s, idx, "INV-1", as_of="2026-10-08")["reason"] == "subscription_scope_not_read"


def test_model_cannot_invent_month_plan_filters_or_choose_among_multiple_invoices():
    idx = scope_index()
    assert resolve_invoice_target(state("请处理多扣款。"), idx, "INV-1")["reason"] == "ambiguous_customer_invoice_scope"
    assert resolve_invoice_target(state("请处理Pro多扣款。", scope_month="2026-10", scope_plan="Pro"), idx, "INV-1")["reason"] == "scope_month_not_supported_by_customer"
    assert resolve_invoice_target(state("请处理本月多扣款。", scope_plan="Pro"), idx, "INV-1")["reason"] == "scope_plan_not_supported_by_customer"
    del idx.payments_by_invoice["INV-OLD"]
    assert resolve_invoice_target(state("请处理多扣款。"), idx, "INV-1")["reason"] == "target_scope_incomplete"


def batch(**changes):
    return build_batch_proposal(**{**dict(case_id="CASE-1", customer_id="C-1", verdicts=money(count=3).items,
                                          evidence_versions={"invoice_refunds": "all-records", "target_scope": "all-candidates"}, version=1,
                                          target_resolution={"scope": "invoice", "invoice_id": "INV-1"},
                                          clock=lambda: datetime(2026, 10, 8, tzinfo=timezone.utc)), **changes})


def test_batch_has_full_stable_child_proposals_and_separate_per_payment_disputes():
    proposal = batch()
    assert proposal["kind"] == "batch_refund" and proposal["amount_cents"] == 9800
    assert proposal["payment_id"] == "PAY-2"  # compatibility display; NOT amount target
    assert [p["payment_id"] for p in proposal["items"]] == ["PAY-2", "PAY-3"]
    assert all(p["amount_cents"] == 4900 and p["version"] == 1 for p in proposal["items"])
    assert len({p["dispute_key"] for p in proposal["items"]}) == 2
    assert all(p["evidence_versions"]["batch_context"] for p in proposal["items"])
    assert proposal == batch()
    assert batch(clock=lambda: datetime(2026, 10, 9, tzinfo=timezone.utc))["action_id"] == proposal["action_id"]


@pytest.mark.parametrize("change", ["amount", "item", "order", "version", "scope", "confirmation", "evidence"])
def test_any_batch_approval_condition_change_invalidates_parent_and_all_child_action_keys(change):
    original, args = batch(), {}
    verdicts = list(money(count=3).items)
    if change == "amount":
        args["verdicts"] = [verdicts[0], replace(verdicts[1], amount_cents=1)]
    elif change == "item":
        args["verdicts"] = [verdicts[0], replace(verdicts[1], payment_id="PAY-4")]
    elif change == "order":
        args["verdicts"] = list(reversed(verdicts))
    elif change == "version":
        args["version"] = 2
    elif change == "scope":
        args["target_resolution"] = {"scope": "explicit_payment", "invoice_id": "INV-1"}
    elif change == "confirmation":
        args["target_resolution"] = {"scope": "invoice", "invoice_id": "INV-1", "customer_confirmation": {"scope_digest": "digest", "command_id": "CMD-9"}}
    else:
        args["evidence_versions"] = {"invoice_refunds": "new-records", "target_scope": "all-candidates"}
    changed = batch(**args)
    assert original["fingerprint"] != changed["fingerprint"]
    assert original["action_id"] != changed["action_id"]
    assert not {p["action_id"] for p in original["items"]} & {p["action_id"] for p in changed["items"]}


def test_batch_revalidation_is_for_all_items_before_execution_not_only_remaining_items():
    proposal, verdicts = batch(), list(money(count=3).items)
    assert same_batch_conditions(proposal, verdicts)
    assert not same_batch_conditions(proposal, verdicts[:1])
    assert not same_batch_conditions(proposal, [replace(verdicts[0], amount_cents=1), verdicts[1]])
    assert not same_batch_conditions(proposal, [verdicts[0], verdicts[0]])
    tampered = deepcopy(proposal)
    tampered["amount_cents"] += 1
    assert not same_batch_conditions(tampered, verdicts)
    with pytest.raises(ValueError, match="duplicate"):
        batch(verdicts=[verdicts[0], verdicts[0]])
    with pytest.raises(ValueError, match="one invoice"):
        batch(verdicts=[verdicts[0], replace(verdicts[1], invoice_id="INV-OTHER")])
