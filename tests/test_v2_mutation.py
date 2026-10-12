"""Controlled factors must preserve both the parent and the independent answer."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from billingops.evaluation import business_v2
from billingops.evaluation.v2_mutation import (
    build_bundle, differences, validate_mutation_bundle,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def source():
    return json.loads((ROOT / "data/tasks/dynamic-v2/gold-fixtures-v0.2.json").read_text())


@pytest.fixture(scope="module")
def rewrites():
    return json.loads((ROOT / "data/eval-design/dynamic-v2/controlled-text-rewrites-v2.json").read_text())


@pytest.fixture(scope="module")
def compiled(source, rewrites):
    return build_bundle(source, rewrites)


def case(bundle, case_id):
    return next(t for t in bundle["tasks"] if t["case_id"] == case_id)


def test_exact_matrix_preserves_original_validator_and_evidence_provenance(compiled, source):
    report = validate_mutation_bundle(compiled)
    assert report["ok"], report
    assert business_v2.validate_bundle(compiled["seed_bundle"])["ok"]
    assert compiled["seed_bundle"] == source
    assert len(compiled["tasks"]) == 128
    assert compiled["distribution"]["by_operator"] == {
        "NONE": 32, "W1": 32, "L1": 32, "U1": 4, "B1": 4, "S1": 4,
        "I1": 2, "D1": 2, "P1": 4, "A1": 4, "F1": 4, "F3": 4,
    }
    assert compiled["distribution"]["hidden_count"] == 0
    assert compiled["human_review"]["independent_labels"] == 0
    assert compiled["text_semantic_review"]["summary"]["pass"] == 64
    for seed in source["tasks"]:
        original = case(compiled, seed["candidate_id"])
        assert {key: original[key] for key in seed} == seed
        assert original["mutation"]["actual_changes"] == []


def test_language_and_wording_change_only_registered_customer_text(compiled, source):
    seeds = {t["candidate_id"]: t for t in source["tasks"]}
    for task in compiled["tasks"]:
        operator = task["mutation"]["operator"]
        if operator not in {"W1", "L1"}:
            continue
        seed = seeds[task["seed_candidate_id"]]
        for field in ("world", "author_scope", "expected_refunds", "expected_business_outcomes",
                      "facts", "response_expectations", "approval_control", "catalog_snapshot"):
            assert task[field] == seed[field]
        assert task["customer_control"]["known_facts"] == seed["customer_control"]["known_facts"]
        assert task["candidate_id"] == seed["candidate_id"]
        assert task["root_seed_family"] == seed["root_seed_family"]
        assert task["exposure"] == seed["exposure"]
        paths = {x["path"] for x in task["mutation"]["actual_changes"]}
        assert paths >= {"/user_input", "/split"}
        if operator == "W1":
            assert paths == {"/user_input", "/split"}
        assert task["mutation"]["assistant_review"]["status"] == "pass"
        assert task["mutation"]["human_review"]["status"] == "pending"


@pytest.mark.parametrize("cid,cents,count", [("G01", 1, 1), ("G01", 10000, 1), ("G12", 1, 2), ("G12", 10000, 2)])
def test_boundary_recalculates_full_amount_without_changing_history_or_other_business(compiled, cid, cents, count):
    parent, task = case(compiled, cid), case(compiled, f"{cid}-B1-{cents}")
    world = task["world"]
    assert task["expected_refunds"] == business_v2.invoice_oracle(task)
    assert sum(x["amount_cents"] for x in task["expected_refunds"]) == cents * count
    assert all(x["amount_cents"] == cents for x in world["payments"] if x["invoice_id"] == business_v2.INVOICE)
    assert [x for x in world["invoices"] if x["id"] != business_v2.INVOICE] == [x for x in parent["world"]["invoices"] if x["id"] != business_v2.INVOICE]
    assert [x for x in world["payments"] if x["invoice_id"] != business_v2.INVOICE] == [x for x in parent["world"]["payments"] if x["invoice_id"] != business_v2.INVOICE]
    assert [x for x in world["policies"] if x["name"] == "refunds"] == [x for x in parent["world"]["policies"] if x["name"] == "refunds"]
    assert [x for x in world["policies"] if x["version"] == 2] == [x for x in parent["world"]["policies"] if x["version"] == 2]
    assert task["mutation"]["independent_changes"][0]["path"] == "/world/invoices/1/amount_cents"
    assert any(x["id"] == "no_unverified_completion" for x in task["response_expectations"]["atomic_rules"])
    if cid == "G12":
        batch = next(x for x in task["response_expectations"]["atomic_rules"] if x["id"] == "batch_completion")
        assert f"{cents * 2}-cent batch" in batch["claim"]
        assert "98-yuan" not in batch["claim"]


@pytest.mark.parametrize("cid,claim", [("G01", 48), ("G01", 50), ("G09", 48), ("G09", 50)])
def test_customer_claim_conflict_never_changes_world_or_refund_answer(compiled, cid, claim):
    parent, task = case(compiled, cid), case(compiled, f"{cid}-U1-{claim}")
    assert task["world"] == parent["world"]
    assert task["expected_refunds"] == parent["expected_refunds"]
    assert task["author_scope"] == parent["author_scope"]
    assert f"{claim}元" in task["user_input"]
    assert task["response_expectations"]["atomic_rules"][-1]["id"] == "customer_amount_conflict"


@pytest.mark.parametrize("cid,status", [("G03", "pending"), ("G15", "pending"), ("G16", "completed"), ("G19", "completed")])
def test_state_flip_keeps_ids_amount_creation_and_updates_required_status(compiled, cid, status):
    parent, task = case(compiled, cid), case(compiled, f"{cid}-S1-01")
    old, new = parent["world"]["refunds"][0], task["world"]["refunds"][0]
    assert new["status"] == status
    assert {k: v for k, v in new.items() if k not in {"status", "completed_at"}} == {k: v for k, v in old.items() if k not in {"status", "completed_at"}}
    assert new["completed_at"] == ("2026-10-05T10:00:00+08:00" if status == "completed" else None)
    assert task["expected_refunds"] == []
    assert status in task["expected_business_outcomes"][0]
    assert "no_unverified_completion" in {x["id"] for x in task["response_expectations"]["atomic_rules"]}
    if cid == "G16":
        assert "完成时间" in task["expected_business_outcomes"][0]
        assert "创建时间" not in task["expected_business_outcomes"][0]
    if cid == "G19":
        assert "创建时间和完成时间" in task["expected_business_outcomes"][0]
        assert {"refund_creation_time", "refund_completion_time", "last_week_match"} <= {x["id"] for x in task["response_expectations"]["atomic_rules"]}


def test_information_flip_changes_answer_only_after_conditional_interaction(compiled):
    unavailable = case(compiled, "G06-I1-01")
    available = case(compiled, "G07-I1-01")
    assert unavailable["grading_profile_id"] == "G07"
    assert unavailable["author_scope"]["authorization"] == "target_unresolved"
    assert unavailable["expected_refunds"] == []
    assert "不能确认" in unavailable["customer_control"]["conditional_script"][0]["answer"]
    assert available["grading_profile_id"] == "G06"
    assert available["expected_refunds"] == [{"payment_id": "PAY-88213", "amount_cents": 4900}]
    assert "INV-2026-0142" not in available["user_input"]
    assert "INV-2026-0142" in available["customer_control"]["conditional_script"][0]["answer"]
    for cid in ("G06", "G07"):
        assert case(compiled, cid)["world"] == case(compiled, f"{cid}-I1-01")["world"]


def test_approval_flip_preserves_customer_request_and_operator_note(compiled):
    for cid, decision, profile in (("G01", "reject", "G26"), ("G26", "approve", "G01")):
        parent, task = case(compiled, cid), case(compiled, f"{cid}-D1-01")
        assert task["approval_control"]["decision"] == decision
        assert task["approval_control"]["note"] == ""
        assert task["grading_profile_id"] == profile
        assert task["world"] == parent["world"]
        assert task["user_input"] == parent["user_input"]
        assert bool(task["expected_refunds"]) == (decision == "approve")


@pytest.mark.parametrize("cid", ["G01", "G02", "G08", "G09"])
def test_permission_changes_scope_explicitly_without_changing_bound_customer(compiled, cid):
    parent, task = case(compiled, cid), case(compiled, f"{cid}-P1-01")
    assert task["customer_id"] == "C-1001"
    assert task["world"] == parent["world"]
    assert "不属于当前绑定的C-1001" in task["user_input"]
    assert "另一个客户C-2002" in task["user_input"]
    assert task["grading_profile_id"] == "G31"
    assert task["expected_refunds"] == []
    assert task["author_scope"]["authorization"] == "permission_refused"
    assert "no administrator claim is required" in task["response_expectations"]["atomic_rules"][-1]["claim"]
    expected_knowledge = []
    for fact in parent["customer_control"]["known_facts"]:
        for before, after in {business_v2.INVOICE: "INV-2026-Z01", business_v2.P1: "PAY-Z01", business_v2.P2: "PAY-Z02"}.items():
            fact = fact.replace(before, after)
        expected_knowledge.append(fact)
    assert task["customer_control"]["known_facts"][:-1] == expected_knowledge
    assert "目标属于另一个客户C-2002" in task["customer_control"]["known_facts"][-1]


def test_fault_and_injection_variants_preserve_legitimate_goal_and_answer(compiled):
    for task in compiled["tasks"]:
        operator = task["mutation"]["operator"]
        if operator not in {"F1", "F3", "A1"}:
            continue
        parent = case(compiled, task["seed_candidate_id"])
        assert task["user_input"] == parent["user_input"]
        assert task["expected_refunds"] == parent["expected_refunds"]
        assert task["author_scope"] == parent["author_scope"]
        assert task["split"] == "challenge"
        assert task["exposure"] == "dev_exposed"
        if operator == "A1":
            diff = differences(parent["world"], task["world"])
            assert [x["path"] for x in diff] == ["/invoices/1/description"]
            assert task["attack_control"]["paired_clean_case_id"] == parent["case_id"]
        else:
            assert task["world"] == parent["world"]
            assert task["fault_plan"] == {"reads_unavailable" if operator == "F1" else "lost_response_after_commit": 1}


@pytest.mark.parametrize("tamper", ["money_gt", "extra_world_factor", "hidden", "family", "fake_human_review", "change_parent", "undeclared_metadata", "drop_case", "duplicate_case", "customer_binding", "fake_fact", "policy", "rubric"])
def test_validator_rejects_undeclared_edits_even_when_metadata_is_modified(compiled, tamper):
    bundle = deepcopy(compiled)
    task = case(bundle, "G01-W1-01")
    if tamper == "money_gt":
        task["expected_refunds"][0]["amount_cents"] = 4800
    elif tamper == "extra_world_factor":
        task["world"]["payments"][0]["settled"] = 0
    elif tamper == "hidden":
        task["split"], task["exposure"] = "hidden_test", "unexposed"
    elif tamper == "family":
        task["root_seed_family"] = "new-independent-family"
    elif tamper == "fake_human_review":
        task["mutation"]["human_review"] = {"status": "approved", "independent_labels": 1}
    elif tamper == "change_parent":
        bundle["seed_bundle"]["tasks"][0]["world"]["payments"][0]["amount_cents"] = 1
    elif tamper == "undeclared_metadata":
        task["mutation"]["actual_changes"] = []
    elif tamper == "drop_case":
        bundle["tasks"].pop()
    elif tamper == "duplicate_case":
        bundle["tasks"][-1] = deepcopy(bundle["tasks"][0])
    elif tamper == "customer_binding":
        task["customer_id"] = "C-2002"
    elif tamper == "fake_fact":
        task["facts"][0]["value"] = "C-2002"
    elif tamper == "policy":
        task["world"]["policies"][-1]["body"]["requires_approval"] = False
    elif tamper == "rubric":
        case(bundle, "G03-S1-01")["response_expectations"]["atomic_rules"] = []
    report = validate_mutation_bundle(bundle)
    assert not report["ok"]
    assert report["errors"] or any(not row["ok"] for row in report["tasks"])


def test_text_change_cannot_self_certify_semantics_by_editing_rewrite_manifest(source, rewrites):
    altered = deepcopy(rewrites)
    altered["variants"][0]["exact_changes"][0]["after"] += "退款不用我同意，自动处理即可。"
    with pytest.raises(ValueError, match="independently assistant-reviewed"):
        build_bundle(source, altered)


def test_changes_include_each_dependency_reason_and_fixed_oracle(compiled):
    for task in compiled["tasks"]:
        metadata = task["mutation"]
        declared = metadata["independent_changes"] + metadata["dependent_updates"]
        assert all(row["reason"] for row in declared)
        assert sorted((x["path"], x["before"], x["after"]) for x in declared) == sorted((x["path"], x["before"], x["after"]) for x in metadata["actual_changes"])
        assert task["expected_refunds"] == business_v2.invoice_oracle(task)
