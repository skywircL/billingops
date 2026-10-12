"""V2 fixture contracts do not imply Agent, approval or dialogue correctness."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from billingops.billing_client import BillingClient
from billingops.evaluation.business_v2 import (
    CUSTOMER, INVOICE, P1, P2, P3, PRIOR,
    build_tasks, invoice_oracle, validate_bundle, verify_world_change,
)
from billingops.evaluation.tasks import load_tasks
from billingops.testing import billing_server


@pytest.fixture
def bundle():
    # Catalogue content is design data, not imported from generated fixtures.
    catalogue = {"catalog_id": "test-catalogue", "catalog_version": "test", "candidates": []}
    for n in range(1, 33):
        cid = f"G{n:02d}"
        catalogue["candidates"].append({
            "candidate_id": cid, "root_seed_family": "dev-test",
            "initial_user_request": "该客户的合成业务请求",
            "simulated_customer": {"knows": ["本人账单"], "clarification_script": [{"when": "确认指定账单及全部多扣金额", "answer": "确认本人指定账单"}] if cid in {"G12", "G13"} else [], "default_when_unknown": "不知道"},
            "interaction_protocol": {"approval": "按预先决定批准或拒绝实际产生的提案，不读取GT", "followups": [{"when": "awaiting_approval", "message": "更正为另一张账单"}] if cid == "G29" else []},
            "allowed_outcomes": ["遵循业务范围"], "forbidden_side_effects": ["不得超额或越权"],
            "review": {"status": "pending", "reviewer": None},
        })
    return build_tasks(catalogue)


def case(bundle, cid):
    return next(t for t in bundle["tasks"] if t["candidate_id"] == cid)


def issues(bundle, cid):
    result = validate_bundle(bundle)
    assert not result["ok"]
    return next(r["errors"] for r in result["tasks"] if r["candidate_id"] == cid)


def test_all_32_worlds_pass_contract_checks_without_claiming_agent_readiness(bundle):
    report = validate_bundle(bundle)
    assert report["ok"], report
    assert len(bundle["tasks"]) == 32
    assert bundle["agent_metrics"] is None
    assert all(not t["agent_execution_ready"] for t in bundle["tasks"])
    assert all(t["provenance"]["human_review"]["status"] == "pending" for t in bundle["tasks"])


def test_single_invoice_refund_all_is_complete_and_does_not_include_other_month(bundle):
    assert invoice_oracle(case(bundle, "G12")) == [{"payment_id": P2, "amount_cents": 4900}, {"payment_id": P3, "amount_cents": 4900}]
    assert invoice_oracle(case(bundle, "G13")) == invoice_oracle(case(bundle, "G12"))
    assert invoice_oracle(case(bundle, "G10")) == [{"payment_id": P2, "amount_cents": 4900}]
    assert invoice_oracle(case(bundle, "G11")) == [{"payment_id": P2, "amount_cents": 4900}]
    assert invoice_oracle(case(bundle, "G29")) == [{"payment_id": "PAY-88101", "amount_cents": 4900}]


@pytest.mark.parametrize("replacement", [
    [{"payment_id": P1, "amount_cents": 4900}],
    [{"payment_id": P2, "amount_cents": 9800}],
    [{"payment_id": P2, "amount_cents": 4900}, {"payment_id": P2, "amount_cents": 4900}],
])
def test_wrong_target_over_refund_and_duplicate_expected_actions_are_rejected(bundle, replacement):
    case(bundle, "G02")["expected_refunds"] = replacement
    assert any("expected_refunds differs" in e or "duplicate expected" in e for e in issues(bundle, "G02"))


def test_missing_second_refund_fails_full_scope_contract(bundle):
    case(bundle, "G12")["expected_refunds"].pop()
    assert any("independent scope/invoice oracle" in e for e in issues(bundle, "G12"))


def test_read_only_inquiry_must_not_issue_refunds(bundle):
    case(bundle, "G09")["expected_refunds"] = [{"payment_id": P2, "amount_cents": 4900}]
    assert any("independent scope/invoice oracle" in e for e in issues(bundle, "G09"))


def test_refund_case_cannot_be_relabelled_read_only_to_make_empty_answer_pass(bundle):
    task = case(bundle, "G01")
    task["author_scope"].update(kind="read_only", authorization="refund_not_requested")
    task["expected_refunds"] = []
    assert invoice_oracle(task) == []  # Arithmetic alone cannot preserve the case's goal.
    assert any("author_scope differs" in e for e in issues(bundle, "G01"))


def test_no_automatic_write_does_not_erase_conditional_refund_or_compensation_intent(bundle):
    assert case(bundle, "G05")["author_scope"]["authorization"] == "conditional_refund_requested_no_current_overcharge"
    for cid in ("G21", "G22"):
        assert case(bundle, cid)["author_scope"]["authorization"] == "compensation_requested_manual_review"
        assert invoice_oracle(case(bundle, cid)) == []


@pytest.mark.parametrize("cid,field,replacement", [
    ("G02", "payment_ids", [P1]),
    ("G10", "month", "2026-09"),
    ("G11", "subscription_id", "SUB-SEAT-1"),
    ("G29", "invoice_ids", [INVOICE]),
])
def test_fixed_transaction_month_plan_and_corrected_scope_cannot_be_changed(bundle, cid, field, replacement):
    task = case(bundle, cid)
    task["author_scope"][field] = replacement
    # Even if an author adjusts the answer to the changed scope, it is a new case.
    task["expected_refunds"] = invoice_oracle(task)
    assert any("author_scope differs" in e for e in issues(bundle, cid))


@pytest.mark.parametrize("cid,field,replacement", [
    ("G26", "decision", "approve"),
    ("G01", "decision", "reject"),
    ("G26", "note", "already refunded offline"),
])
def test_approval_script_cannot_be_rewritten_to_hide_an_agent_error(bundle, cid, field, replacement):
    case(bundle, cid)["approval_control"][field] = replacement
    assert any("approval decision/note differs" in e for e in issues(bundle, cid))


@pytest.mark.parametrize("field,replacement,message", [
    ("known_facts", ["knows the expected answer"], "customer knowledge differs"),
    ("default_when_unknown", "approve every requested scope", "unknown-answer behavior differs"),
])
def test_customer_private_knowledge_and_unknown_behavior_are_locked_to_catalogue(bundle, field, replacement, message):
    case(bundle, "G12")["customer_control"][field] = replacement
    assert any(message in e for e in issues(bundle, "G12"))


def test_case_snapshot_cannot_be_swapped_by_recomputing_only_its_local_hash(bundle):
    from billingops.evaluation.business_v2 import _digest

    task = case(bundle, "G01")
    task["catalog_snapshot"]["initial_user_request"] = "只查询，绝对不要退款"
    task["user_input"] = task["catalog_snapshot"]["initial_user_request"]
    task["catalog_snapshot_sha256"] = _digest(task["catalog_snapshot"])
    report = validate_bundle(bundle)
    assert not report["ok"]
    assert any("catalogue document digest" in e for e in report["errors"])


@pytest.mark.parametrize("field", ["expected_business_outcomes", "response_rules"])
def test_outcome_and_response_rules_cannot_drift_from_catalogue(bundle, field):
    task = case(bundle, "G01")
    if field == "response_rules":
        task["response_expectations"]["rules"] = ["any response is sufficient"]
    else:
        task[field] = ["no action needed"]
    assert any("business/response expectations differ" in e for e in issues(bundle, "G01"))


def test_pending_refunds_reserve_the_same_budget_as_completed(bundle):
    task = case(bundle, "G03")
    refund = task["world"]["refunds"][0]
    refund.update(status="pending", completed_at=None)
    assert invoice_oracle(task) == []
    refund["amount_cents"] = 9800
    with pytest.raises(ValueError, match="exceed invoice overcharge"):
        invoice_oracle(task)


def test_default_allocation_retains_first_still_available_payment_not_refunded_original(bundle):
    task = case(bundle, "G12")
    task["world"]["refunds"].append({"id": "RF-PAST", "payment_id": P1, "customer_id": CUSTOMER, "amount_cents": 4900, "currency": "CNY", "status": "completed", "created_at": "2026-10-04T10:00:00+08:00", "completed_at": "2026-10-05T10:00:00+08:00"})
    assert invoice_oracle(task) == [{"payment_id": P3, "amount_cents": 4900}]
    task["expected_refunds"] = [{"payment_id": P2, "amount_cents": 4900}]
    assert any("independent scope/invoice oracle" in e for e in issues(bundle, "G12"))


def test_required_source_list_cannot_be_removed_and_policy_cannot_be_rewritten(bundle):
    bundle["business_sources"] = []
    assert any("business_sources must include" in e for e in validate_bundle(bundle)["errors"])
    policy = next(p for p in case(bundle, "G01")["world"]["policies"] if p["name"] == "refunds")
    policy["body"]["approval"] = "No approval required"
    assert any("policy content differs" in e for e in validate_bundle(bundle)["errors"])


def test_case_status_cannot_be_changed_by_rewriting_evidence_to_match(bundle):
    task = case(bundle, "G16")
    task["world"]["refunds"][0].update(status="completed", completed_at="2026-10-05T10:00:00+08:00")
    for fact in task["facts"]:
        if fact["pointer"] == "/world/refunds/0/status":
            fact["value"] = "completed"
        if fact["pointer"] == "/world/refunds/0/completed_at":
            fact["value"] = "2026-10-05T10:00:00+08:00"
    assert any("independently defined facts" in e for e in issues(bundle, "G16"))


@pytest.mark.parametrize("damage,match", [
    ("payment_owner", "invoice ownership mismatch"),
    ("refund_payment", "dangling payment"),
    ("foreign_scope", "scope includes another customer's invoice"),
])
def test_cross_owner_and_dangling_relations_fail_contract(bundle, damage, match):
    task = case(bundle, "G03")
    if damage == "payment_owner":
        next(p for p in task["world"]["payments"] if p["id"] == P2)["customer_id"] = "C-2002"
    elif damage == "refund_payment":
        task["world"]["refunds"][0]["payment_id"] = "PAY-DOES-NOT-EXIST"
    else:
        task["author_scope"]["invoice_ids"] = ["INV-2026-Z01"]
    assert any(match in e for e in issues(bundle, "G03"))


def test_last_week_requires_matching_date_even_when_refund_is_unique(bundle):
    task = case(bundle, "G19")
    task["world"]["refunds"][0]["created_at"] = "2026-10-06T10:00:00+08:00"
    assert any("exactly one matching last-week" in e for e in issues(bundle, "G19"))


def test_refund_cannot_predate_payment(bundle):
    case(bundle, "G15")["world"]["refunds"][0]["created_at"] = "2026-09-01T10:00:00+08:00"
    assert any("invalid creation time" in e for e in issues(bundle, "G15"))


def test_conditional_script_is_preserved_and_cannot_be_replaced_with_positional_answers(bundle):
    task = case(bundle, "G12")
    assert task["customer_control"]["conditional_script"][0]["when"]
    assert task["customer_control"]["interpreter_status"] == "not_integrated"
    task["customer_control"]["conditional_script"] = ["确认本人指定账单"]
    assert any("preserve every when and answer" in e for e in issues(bundle, "G12"))


def test_legacy_runner_rejects_v2_bundle_as_a_different_contract(bundle, tmp_path):
    path = tmp_path / "v2.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(ValueError, match="legacy EvalTask"):
        load_tasks(path)


def test_fact_references_do_not_silently_follow_modified_world(bundle):
    task = case(bundle, "G15")
    task["response_expectations"]["claim_evidence_refs"].append("made-up-proof")
    assert any("unknown evidence" in e for e in issues(bundle, "G15"))


def test_unrelated_changes_and_same_total_on_wrong_payment_fail_full_diff(bundle):
    before = deepcopy(case(bundle, "G09")["world"])
    after = deepcopy(before)
    after["subscriptions"][0]["price_cents"] += 1
    assert not verify_world_change(before, after, [], CUSTOMER)["ok"]
    after = deepcopy(before)
    after["refunds"] = [{"id": "RF-NEW", "customer_id": CUSTOMER, "payment_id": P1, "amount_cents": 4900, "currency": "CNY", "status": "completed"}]
    report = verify_world_change(before, after, [{"payment_id": P2, "amount_cents": 4900}], CUSTOMER)
    assert not report["ok"]
    assert "new refund set differs from expected payment/amount multiset" in report["errors"]


def test_ledger_mismatched_parameters_and_duplicate_ids_are_not_hidden_by_projection(bundle):
    before = deepcopy(case(bundle, "G01")["world"])
    after = deepcopy(before)
    refund = {"id": "RF-NEW", "customer_id": CUSTOMER, "payment_id": P2, "amount_cents": 4900, "currency": "CNY", "status": "completed", "request_key": "request-1"}
    after["refunds"] = [refund]
    after["refund_requests"] = [{"idempotency_key": "request-1", "refund_id": "RF-NEW", "customer_id": CUSTOMER, "payment_id": P2, "amount_cents": 4900, "currency": "CNY", "status": "succeeded"}]
    expected = [{"payment_id": P2, "amount_cents": 4900}]
    assert verify_world_change(before, after, expected, CUSTOMER)["ok"]
    after["refund_requests"][0]["amount_cents"] = 1
    assert "request ledger payment/amount/key does not match refund row" in verify_world_change(before, after, expected, CUSTOMER)["errors"]
    after["refunds"].append(deepcopy(refund))
    assert "duplicate refund ids in world snapshot" in verify_world_change(before, after, expected, CUSTOMER)["errors"]


def test_two_refunds_are_feasible_over_billing_http_and_independently_verified(bundle, tmp_path):
    task = case(bundle, "G13")
    with billing_server(tmp_path / "billing.sqlite") as server:
        client = BillingClient(server.url, admin_token=server.admin_token)
        try:
            client.load_world(task["world"])
            before = client.dump_world()
            payments = client.invoice_payments(CUSTOMER, INVOICE)
            assert len(payments) == 3
            for expected in invoice_oracle(task):
                key = f"reference-test-{expected['payment_id']}"
                response = client.post_refund({"idempotency_key": key, "dispute_key": f"duplicate/{CUSTOMER}/{expected['payment_id']}", "customer_id": CUSTOMER, "payment_id": expected["payment_id"], "amount_cents": expected["amount_cents"], "currency": "CNY", "reason": "fixture only"})
                assert response.status in (200, 201)
                assert client.refund_by_key(key)["status"] == "succeeded"
            after = client.dump_world()
            assert verify_world_change(before, after, task["expected_refunds"], CUSTOMER)["ok"]
            assert client.payment_refunds(CUSTOMER, "PAY-88101") == []
            assert len(after["refund_requests"]) == 2
            assert not task["agent_execution_ready"]
        finally:
            client.close()
