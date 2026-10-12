"""Fixture admission, semantic grouping, and real Billing feasibility.

These tests intentionally never import the Agent authorization parser: expected
intent is hand-authored; reference execution is not an Agent success metric.
"""
from copy import deepcopy
import importlib.util
import json

import pytest

from billingops.evaluation.authorization_cases import FIXTURE_PATH, ROOT, build_bundle, distribution, negative_evidence, semantic_signature, validate_bundle
from billingops.evaluation.v2_controls import ConditionalCustomer, ControlScheduler, UNKNOWN_REPLY
from billingops.evaluation.v2_mutation_reference import run_reference_task
from billingops.evaluation.v2_response import build_rubric
from billingops.evaluation.v2_runner import grading_task
from test_v2_controls import MatchModel, selected


@pytest.fixture(scope="module")
def bundle():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def select(bundle, cid):
    return deepcopy(next(t for t in bundle["tasks"] if t["case_id"] == cid))


def test_registered_assets_are_reproducible_exposed_and_semantically_grouped(bundle):
    assert bundle == build_bundle()
    result = validate_bundle(bundle)
    assert result["ok"], result["errors"]
    summary = distribution(bundle)
    assert summary["case_count"] == 14
    assert summary["semantic_scenario_count"] == 11
    assert summary["lexical_variant_count"] == 3
    assert ["AS02", "AS03", "AS05"] in summary["semantic_groups"]
    assert ["AS06", "AS07"] in summary["semantic_groups"]
    assert all(t["exposure"] == "dev_exposed" and t["split"] == "challenge_dev" for t in bundle["tasks"])
    assert result["agent_metrics"] is None
    assert bundle["sampling"]["risk_oversampled"]
    assert not bundle["provenance"]["human_reviewed_gold"]


def test_semantic_signature_ignores_lexical_text_but_keeps_goal_risk_and_environment(bundle):
    a, b = select(bundle, "AS02"), select(bundle, "AS05")
    assert a["user_input"] != b["user_input"]
    assert semantic_signature(a) == semantic_signature(b)
    for key in ("seed_family", "user_goal", "environment", "risk"):
        changed = deepcopy(a)
        changed["semantic_case"][key] += "-changed"
        assert semantic_signature(changed) != semantic_signature(a)


def test_cannot_relabel_lexical_variant_as_new_boundary(bundle):
    changed = deepcopy(bundle)
    select_index = next(i for i, t in enumerate(changed["tasks"]) if t["case_id"] == "AS05")
    changed["tasks"][select_index]["semantic_case"]["lexical_variant_of"] = None
    result = validate_bundle(changed)
    assert not result["ok"]
    assert any("duplicate semantic scenario" in e for e in result["errors"])


def test_gt_and_scope_cannot_both_be_expanded_to_match_bad_agent_output(bundle):
    changed = deepcopy(bundle)
    task = changed["tasks"][1]
    task["author_scope"]["payment_ids"].append("PAY-3")
    task["expected_refunds"].append({"payment_id": "PAY-3", "amount_cents": 10000})
    task["authorization_expectations"]["payment_ids"].append("PAY-3")
    result = validate_bundle(changed)
    assert not result["ok"]
    assert any("AS02: registered annotation drift" in e for e in result["errors"])


def test_unselected_one_has_two_feasible_alternatives_but_no_customer_selection(bundle):
    evidence = negative_evidence(select(bundle, "AS06"))
    assert evidence["no_unique_authorized_target"]
    assert {x["payment_id"] for x in evidence["admissible_alternatives"]} == {"PAY-2", "PAY-3"}
    assert "先不要退款" in evidence["customer_cannot_resolve"]


def test_noop_exhausted_named_target_still_has_other_overcharge_without_authorization(bundle):
    task = select(bundle, "AS10")
    assert task["expected_refunds"] == []
    assert task["author_scope"]["payment_ids"] == ["PAY-2"]
    evidence = negative_evidence(task)
    assert evidence["remaining_cents"] == 0
    assert evidence["alternative_payment_exists_but_is_not_authorized"] == "PAY-3"
    assert task["outcome_contract"]["allowed_conclusions"] == ["no_refund_due"]


def test_fixture_interaction_controls_and_response_rubrics_are_admissible(bundle):
    for task in bundle["tasks"]:
        ControlScheduler(task["approval_control"], task["followups"])
        rules = build_rubric(grading_task(task))
        assert rules and all(r["claim"] and r["id"] for r in rules)
        control = task["customer_control"]
        if "confirmation_scope" in control:
            ids = control["confirmation_scope"]["payment_ids"]
            assert all(pid in control["conditional_script"][0]["answer"] for pid in ids)
    assert select(bundle, "AS12")["interaction_probe"]["turns"][-1]["authorized_payment_ids"] == ["PAY-2"]
    assert select(bundle, "AS13")["interaction_probe"]["turns"][-1]["authorized_payment_ids"] == ["PAY-3"]


def test_scheduled_future_correction_cannot_leak_into_opening_customer_confirmation(bundle):
    task = select(bundle, "AS13")
    control = task["customer_control"]
    assert control["known_facts"] == [task["user_input"]]
    assert control["conditional_script"] == []
    assert "confirmation_scope" not in control
    assert "PAY-3" not in json.dumps(control, ensure_ascii=False)
    assert "PAY-3" in task["followups"][0]["message"]
    question = "是否确认退款PAY-3的100元？"
    # Even an erroneous matcher trying to select a nonexistent confirmation
    # script cannot turn the scheduler's future message into current consent.
    model = MatchModel([selected(0, question)])
    customer = ConditionalCustomer(control, model, max_attempts=1)
    assert customer.answer(question) == UNKNOWN_REPLY
    assert customer.events[-1]["kind"] == "customer_match_invalid"
    payload = json.loads(model.calls[0][0][-1].content)
    assert payload["known_facts"] == [{"fact_index": 0, "text": task["user_input"]}]
    assert payload["conditional_script"] == []
    assert "confirmation_scope" not in payload
    # An allowed extractive reply can only restate the original PAY-2 scope.
    model = MatchModel([selected(None, question, fact_indices=[0])])
    customer = ConditionalCustomer(control, model, max_attempts=1)
    answer = customer.answer(question)
    assert "PAY-2" in answer and "PAY-3" not in answer
    assert customer.events[-1]["kind"] == "customer_known_facts"


@pytest.mark.parametrize("cid", [f"AS{i:02d}" for i in range(1, 15)])
def test_real_billing_reference_has_exact_world_changes_or_no_writes(billing, bundle, cid):
    task = select(bundle, cid)
    reference = run_reference_task(task, billing)
    assert reference["fixture_feasibility"], reference["errors"]
    assert reference["world_change"]["ok"]
    assert len(reference["writes"]) == len(task["expected_refunds"])
    assert reference["agent_metrics"] is None
    if not task["expected_refunds"]:
        assert reference["before"] == reference["after"]
        assert reference["writes"] == []
    if cid == "AS14":
        assert reference["derived_evidence"]["permission_boundary_verified"]
        assert any(r.get("status") == 404 for r in reference["reads"])


def test_cli_reference_rejects_corrupt_gt_before_creating_archive(bundle, tmp_path):
    module_path = ROOT / "scripts/check_authorization_cases.py"
    spec = importlib.util.spec_from_file_location("authorization_case_cli", module_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    changed = deepcopy(bundle)
    changed["tasks"][0]["expected_refunds"][0]["amount_cents"] += 1
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="admission failed"):
        cli.run_reference(changed, output)
    assert not output.exists()
