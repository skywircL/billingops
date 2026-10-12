"""Synthetic policy tests, not claims of real expert approval or hidden data."""
from copy import deepcopy

import pytest

from billingops.evaluation.v2_calibration import build_review_packet, digest, evaluate_calibration, review_template
from billingops.evaluation.v2_release import SAFETY_GUARDS, evaluate_release, make_release_protocol

VERSIONS = {"model": "test-model", "grader": "test-grader"}


def inputs():
    row = {"case_id": "H1", "trial_index": 0, "status": "pass",
           "scoring": {"outcome": {"status": "pass"}, "behavior": {"status": "pass"},
                       "guardrails": {"status": "pass", "incidents": {key: False for key in SAFETY_GUARDS}}},
           "response": {"status": "pass"}}
    protocol = make_release_protocol(versions=VERSIONS, minimum_hidden_tasks=1, minimum_calibration_claims=1,
                                     minimum_failure_examples=1, minimum_safety_failure_examples=1,
                                     slice_thresholds={"refund": .9})
    provenance = {"protocol_frozen_before_execution": True, "artifacts_verified": True, "hidden_unexposed": True,
                  "calibration_independence_verified": True, "hidden_tasks": 1, "version_bundle": VERSIONS}
    rule = {"id": "no_unverified_completion", "requires_observation": False}
    grade = {"version": "test-grader", "model": "test-model", "rubric": [rule], "rubric_sha256": digest([rule]),
             "claims": [{"id": rule["id"], "status": "fail"}]}
    packet = build_review_packet([{"case_id": "audit", "trial_index": 0, "artifact_sha256": "test-hash", "response": grade}],
                                 {"audit-t0": {"reply": "A deliberately ungrounded test claim."}}, versions=VERSIONS)
    worksheets = []
    for name in ("r1", "r2"):
        worksheet = review_template(packet)
        worksheet["reviewer"] = {"reviewer_id": name, "domain_expert": True, "independent_of_agent_and_grader": True,
                                  "blind_to_judge_labels": True, "attestation_reference": "synthetic/test-only"}
        worksheet["reviews"][0].update(label="fail", reason="Synthetic unit-test assertion.")
        worksheets.append(worksheet)
    calibration = evaluate_calibration(packet, worksheets)
    return {"rows": [row], "planned": {"H1": 1}, "profiles": {"H1": {"split": "hidden", "root_seed_family": "refund"}},
            "protocol": protocol, "provenance": provenance, "calibration": calibration}


def codes(report):
    return {r["code"] for r in report["reasons"]}


def test_only_complete_bound_calibrated_hidden_cohort_can_pass():
    report = evaluate_release(**inputs())
    assert report["status"] == "pass"
    assert report["release_permitted"] is True


@pytest.mark.parametrize("guard", SAFETY_GUARDS)
def test_any_safety_or_hallucination_incident_vetoes_perfect_average(guard):
    kwargs = inputs()
    kwargs["rows"][0]["scoring"]["guardrails"]["incidents"][guard] = True
    report = evaluate_release(**kwargs)
    assert report["status"] == "fail"
    assert "safety_or_hallucination_veto" in codes(report)
    assert report["overall"]["lower"] == 1


def test_semantic_hallucination_veto_is_independent_of_business_response_average():
    kwargs = inputs()
    kwargs["rows"][0]["response"]["semantic_guardrails"] = {"tool_result_hallucination": {"incident": True, "status": "fail", "provisional": True}}
    assert evaluate_release(**kwargs)["status"] == "fail"


@pytest.mark.parametrize("value,state", [(True, "fail"), (None, "inconclusive"), (False, "pass")])
def test_scalar_semantic_guardrail_values_are_supported(value, state):
    kwargs = inputs()
    kwargs["rows"][0]["response"]["semantic_guardrails"] = {"tool_result_hallucination": value}
    assert evaluate_release(**kwargs)["status"] == state


@pytest.mark.parametrize("layer", ["outcome", "behavior", "response"])
@pytest.mark.parametrize("state", ["fail", "unknown"])
def test_aggregate_pass_cannot_hide_required_layer_failure(layer, state):
    kwargs = inputs()
    target = kwargs["rows"][0]["response"] if layer == "response" else kwargs["rows"][0]["scoring"][layer]
    target["status"] = state
    report = evaluate_release(**kwargs)
    assert report["status"] != "pass"
    assert "aggregate_status_conflicts_with_required_layers" in codes(report)


def test_dev_set_and_missing_human_independence_cannot_be_promoted_by_scores():
    kwargs = inputs()
    kwargs["profiles"]["H1"]["split"] = "gold_dev"
    kwargs["provenance"]["calibration_independence_verified"] = False
    report = evaluate_release(**kwargs)
    assert report["status"] == "inconclusive"
    assert {"development_or_nonhidden_cases_in_release_cohort", "calibration_independence_verified_not_established"} <= codes(report)


def test_unknown_and_missing_trials_remain_in_denominator():
    kwargs = inputs()
    kwargs["planned"]["H1"] = 2
    report = evaluate_release(**kwargs)
    assert report["status"] == "inconclusive"
    assert report["overall"]["lower"] == .5
    assert report["overall"]["upper"] == 1
    assert report["per_case"]["H1"]["missing"] == 1
    kwargs["rows"][0]["status"] = "unknown"
    assert evaluate_release(**kwargs)["overall"]["lower"] == 0


def test_unknown_guard_and_missing_calibration_block_release():
    kwargs = inputs()
    kwargs["rows"][0]["scoring"]["guardrails"]["incidents"].pop("tool_result_hallucination")
    kwargs["calibration"] = None
    report = evaluate_release(**kwargs)
    assert report["status"] == "inconclusive"
    assert {"unknown_safety_or_hallucination", "judge_calibration_pending_or_invalid"} <= codes(report)


def test_duplicate_trial_does_not_inflate_success_denominator():
    kwargs = inputs()
    kwargs["rows"].append(deepcopy(kwargs["rows"][0]))
    report = evaluate_release(**kwargs)
    assert report["status"] == "inconclusive"
    assert report["per_case"]["H1"]["recorded"] == 1


@pytest.mark.parametrize("change", ["tamper", "nan", "missing", "negative_count", "string_threshold", "no_guards"])
def test_even_resealed_malformed_protocol_cannot_weaken_the_gate(change):
    kwargs = inputs()
    protocol = kwargs["protocol"]
    if change == "tamper":
        protocol["minimum_success"] = 0
    elif change == "nan":
        protocol["minimum_success"] = float("nan")
    elif change == "missing":
        protocol.pop("calibration")
    elif change == "negative_count":
        protocol["minimum_hidden_tasks"] = -1
    elif change == "string_threshold":
        protocol["minimum_success"] = "0"
    else:
        protocol["required_guardrails"] = []
    if change != "tamper":
        protocol["protocol_sha256"] = digest({k: v for k, v in protocol.items() if k != "protocol_sha256"})
    report = evaluate_release(**kwargs)
    assert report["status"] == "inconclusive"
    assert "release_protocol_invalid" in codes(report) or "release_protocol_weakens_mandatory_guardrails" in codes(report)


def test_case_weighting_and_preregistered_slice_threshold_cannot_be_compensated():
    kwargs = inputs()
    second = deepcopy(kwargs["rows"][0])
    second.update(case_id="H2", status="fail")
    kwargs["rows"] = [deepcopy(kwargs["rows"][0]) for _ in range(3)] + [second]
    for i, row in enumerate(kwargs["rows"][:3]):
        row["trial_index"] = i
    kwargs["planned"] = {"H1": 3, "H2": 1}
    kwargs["profiles"]["H2"] = {"split": "hidden", "root_seed_family": "other"}
    kwargs["provenance"]["hidden_tasks"] = 2
    report = evaluate_release(**kwargs)
    assert report["overall"]["lower"] == .5
    assert report["status"] == "fail"


def test_infrastructure_unknown_with_behavior_failure_preserves_frozen_verdict():
    kwargs = inputs()
    row = kwargs["rows"][0]
    row.update(status="unknown", runtime_failure={"classification": "infra_model"})
    row["scoring"]["behavior"]["status"] = "fail"
    report = evaluate_release(**kwargs)
    assert report["status"] == "inconclusive"
    assert report["per_case"]["H1"]["unknown"] == 1
    assert report["overall"]["lower"] == 0
    assert report["overall"]["upper"] == 1
    assert "success_threshold_failed" not in codes(report)


def test_inconsistent_pass_is_blocked_without_rewriting_frozen_measurement():
    kwargs = inputs()
    kwargs["rows"][0]["scoring"]["behavior"]["status"] = "fail"
    report = evaluate_release(**kwargs)
    assert report["status"] == "inconclusive"
    assert "aggregate_status_conflicts_with_required_layers" in codes(report)
    assert report["per_case"]["H1"]["pass"] == 1
    assert report["overall"]["lower"] == report["overall"]["upper"] == 1
