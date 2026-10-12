"""Data, simulators, scorer and statistics."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from billingops.evaluation import data as datamod
from billingops.evaluation.data import FAMILIES, generate_dev, generate_heldout
from billingops.evaluation.scorer import score_case
from billingops.evaluation.simulators import DONT_KNOW, ApprovalSimulator, SimulatedUser
from billingops.evaluation.stats import bootstrap_by_family, mcnemar_exact, paired_difference, stratified


def test_heldout_set_has_the_preregistered_strata_and_families() -> None:
    cases = generate_heldout()
    assert len(cases) == 100
    assert Counter(c.category for c in cases) == {"duplicate": 60, "refund_status": 20, "subscription": 20}
    assert {c.family for c in cases} == set(FAMILIES)
    assert len({c.case_id for c in cases}) == 100
    assert len(generate_dev()) == 20


def test_generation_is_deterministic_and_separates_world_disclosable_and_truth() -> None:
    a, b = generate_heldout(), generate_heldout()
    assert [c.to_dict() for c in a] == [c.to_dict() for c in b]
    case = next(c for c in a if c.family == "dup_missing_info_unknown")
    assert "invoice_id" not in case.disclosable  # the user does not know it
    assert case.ground_truth["expected_target_payment_id"] in {p["id"] for p in case.world["payments"]}
    other = next(c for c in a if c.family == "dup_other_customer")
    assert other.disclosable == {} and other.ground_truth["expected_refund_total_cents"] == 0


def test_ground_truth_amounts_do_not_come_from_the_money_module() -> None:
    source = Path(datamod.__file__).read_text(encoding="utf-8")
    import ast

    tree = ast.parse(source)
    imported = {getattr(n, "module", None) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))}
    assert not any(m and m.endswith("money") for m in imported)
    partial = next(c for c in generate_heldout() if c.family == "dup_partial_refund")
    extra = next(p for p in partial.world["payments"] if p["id"] == partial.ground_truth["expected_target_payment_id"])
    prior = sum(r["amount_cents"] for r in partial.world["refunds"])
    assert partial.ground_truth["expected_amount_cents"] == extra["amount_cents"] - prior


def test_simulated_user_only_discloses_allowed_fields() -> None:
    user = SimulatedUser({"invoice_id": "INV-1", "amount_cents": 4900})
    assert "INV-1" in user.answer("Could you share the invoice number?")
    assert user.answer("What is your mother's maiden name?") == DONT_KNOW
    assert user.answer("Which payment id (PAY-...) was it?") == "I only have the invoice: INV-1."
    assert SimulatedUser({}).answer("Could you share the invoice number?") == DONT_KNOW
    sim = ApprovalSimulator()
    assert sim.decide({"amount_cents": 1}) == "approve" and len(sim.approved) == 1


def _view(state: str, conclusion: str, proposal: dict | None, evidence: list, execution: dict | None) -> dict:
    return {"case": {"state": state, "conclusion": {"type": conclusion, "verdict": {"reason": "refundable"}}, "proposal": proposal}, "evidence": evidence, "execution": execution}


TRUTH = {"expected_conclusion": "refund_proposed", "expected_target_payment_id": "PAY-2", "expected_amount_cents": 4900, "required_facts_any": [[{"tool": "list_payments", "args": {"invoice_id": "INV-1"}}, {"tool": "list_refunds", "args": {"payment_id": "PAY-2"}}]], "expected_refund_total_cents": 4900, "expected_final_state": "closed"}
EVIDENCE = [{"tool": "list_payments", "args": {"invoice_id": "INV-1"}, "ok": True}, {"tool": "list_refunds", "args": {"payment_id": "PAY-2"}, "ok": True}]


def test_scorer_requires_conclusion_target_amount_facts_db_and_verification() -> None:
    good = _view("closed", "refund_proposed", {"payment_id": "PAY-2", "amount_cents": 4900}, EVIDENCE, {"status": "done"})
    assert score_case(ground_truth=TRUTH, view=good, refunds_on_target=[{"amount_cents": 4900, "status": "completed"}])["success"]
    unverified = _view("uncertain", "refund_proposed", {"payment_id": "PAY-2", "amount_cents": 4900}, EVIDENCE, {"status": "in_doubt"})
    r = score_case(ground_truth=TRUTH, view=unverified, refunds_on_target=[{"amount_cents": 4900, "status": "completed"}])
    assert not r["success"] and not r["checks"]["executed_and_verified"] and not r["checks"]["final_state"]
    wrong_amount = _view("closed", "refund_proposed", {"payment_id": "PAY-2", "amount_cents": 4000}, EVIDENCE, {"status": "done"})
    assert not score_case(ground_truth=TRUTH, view=wrong_amount, refunds_on_target=[{"amount_cents": 4000, "status": "completed"}])["checks"]["amount"]
    no_facts = _view("closed", "refund_proposed", {"payment_id": "PAY-2", "amount_cents": 4900}, EVIDENCE[:1], {"status": "done"})
    assert not score_case(ground_truth=TRUTH, view=no_facts, refunds_on_target=[{"amount_cents": 4900, "status": "completed"}])["checks"]["facts"]
    escalated = _view("escalated", "escalate", None, EVIDENCE, None)
    assert not score_case(ground_truth=TRUTH, view=escalated, refunds_on_target=[])["success"]  # unnecessary escalation is failure


def test_paired_statistics() -> None:
    pairs = [{"family": f"f{i % 10}", "category": "duplicate" if i < 12 else "status", "a": i % 3 != 0, "b": i % 4 != 0} for i in range(20)]
    diff = paired_difference(pairs)
    assert diff["n"] == 20 and abs(diff["diff"] - (diff["rate_b"] - diff["rate_a"])) < 1e-12
    bs = bootstrap_by_family(pairs, iterations=200)
    assert bs["families"] == 10 and bs["interval"][0] <= diff["diff"] <= bs["interval"][1]
    few = bootstrap_by_family(pairs[:5], iterations=50)
    assert few["degenerate"] and "descriptive" in few["note"]
    mc = mcnemar_exact(pairs)
    assert 0 <= mc["p_value"] <= 1 and mc["a_only"] + mc["b_only"] > 0 and "independent" in mc["assumption"]
    strata = stratified(pairs)
    assert set(strata) == {"duplicate", "status"} and strata["status"]["n"] == 8
