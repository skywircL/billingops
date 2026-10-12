"""Calibration never promotes fabricated, stale, or incomplete human review."""
from copy import deepcopy

import pytest

from billingops.evaluation.v2_calibration import (build_review_packet, digest, evaluate_calibration, review_template)

VERSIONS = {"model": "offline-model", "grader": "test-rubric-1"}


def fixture(label="pass", *, high_risk=True):
    assertion = "no_unverified_completion" if high_risk else "refund_amount"
    rule = {"id": assertion, "claim": "Use the captured receipt", "requires_observation": True}
    raw = {"reply": "The refund was issued.", "view": {"evidence": [{"tool": "read_refund", "data": {"status": "done"}}]}}
    rubric = [rule]
    response = {"version": "rubric-1", "model": "judge-test", "rubric": rubric, "rubric_sha256": digest(rubric),
                "claims": [{"id": assertion, "status": label}], "evidence_index": [{"ref": "/view/evidence/0"}]}
    rows = [{"case_id": "C1", "trial_index": 0, "artifact": "trace.json", "artifact_sha256": "file-hash", "response": response}]
    artifacts = {"C1-t0": raw}
    return build_review_packet(rows, artifacts, versions=VERSIONS), artifacts, rows


def worksheet(packet, name="reviewer-1", label="pass"):
    result = review_template(packet)
    result["reviewer"] = {"reviewer_id": name, "domain_expert": True, "independent_of_agent_and_grader": True,
                          "blind_to_judge_labels": True, "attestation_reference": "review-system/test-signoff"}
    for review in result["reviews"]:
        review.update(label=label, reason="Independently checked the captured receipt.", evidence_refs=["/view/evidence/0"])
    return result


def test_blank_packet_stays_pending_and_template_does_not_leak_model_labels():
    packet, artifacts, _ = fixture()
    template = review_template(packet)
    assert template["reviews"][0]["label"] is None
    assert all("judge_label" not in review and "stratum" not in review for review in template["reviews"])
    report = evaluate_calibration(packet, [], artifacts=artifacts)
    assert report["status"] == "pending"
    assert report["coverage"]["pending"] == 1
    assert report["statistics"]["agreement"]["estimate"] is None


def test_high_risk_requires_distinct_reviewers_and_disagreement_does_not_become_gold():
    packet, artifacts, _ = fixture()
    first = worksheet(packet)
    assert evaluate_calibration(packet, [first], artifacts=artifacts)["status"] == "pending"
    assert evaluate_calibration(packet, [first, first], artifacts=artifacts)["status"] == "invalid"
    disagree = evaluate_calibration(packet, [first, worksheet(packet, "reviewer-2", "fail")], artifacts=artifacts)
    assert disagree["status"] == "pending"
    assert disagree["coverage"]["disagreement"] == 1
    assert disagree["statistics"]["pairs"] == 0


def test_complete_labels_compare_judge_to_human_with_false_negative_visible():
    packet, artifacts, _ = fixture("pass")
    report = evaluate_calibration(packet, [worksheet(packet, "r1", "fail"), worksheet(packet, "r2", "fail")], artifacts=artifacts)
    assert report["status"] == "complete"
    assert report["statistics"]["confusion_matrix"]["fail"]["pass"] == 1
    assert report["statistics"]["failure_recall"]["estimate"] == 0
    assert report["statistics"]["failure_recall"]["n"] == 1
    assert report["statistics"]["failure_precision"]["estimate"] is None
    assert report["statistics"]["agreement"]["upper"] > 0
    assert "not authenticated" in report["identity_assurance"]


def test_judge_abstention_does_not_escape_failure_recall_denominator():
    packet, artifacts, _ = fixture("unknown")
    report = evaluate_calibration(packet, [worksheet(packet, "r1", "fail"), worksheet(packet, "r2", "fail")], artifacts=artifacts)
    assert report["statistics"]["failure_recall"]["estimate"] == 0
    assert report["statistics"]["judge_unknown"] == 1
    assert report["statistics"]["determinate_pairs"] == 0


@pytest.mark.parametrize("change,expected", [
    ("trace", "artifact_digest_mismatch"), ("reference", "invalid_evidence_reference"),
    ("sample", "review_sample_digest_mismatch"), ("identity", "invalid_packet_or_reviewer_declaration"),
])
def test_tampered_or_unsubstantiated_review_is_invalid(change, expected):
    packet, artifacts, _ = fixture(high_risk=False)
    review = worksheet(packet)
    if change == "trace":
        artifacts["C1-t0"]["reply"] = "Another reply"
    elif change == "reference":
        review["reviews"][0]["evidence_refs"] = ["/not/captured"]
    elif change == "sample":
        review["reviews"][0]["sample_sha256"] = "wrong"
    else:
        review["reviewer"]["independent_of_agent_and_grader"] = False
    report = evaluate_calibration(packet, [review], artifacts=artifacts)
    assert report["status"] == "invalid"
    assert expected in [e["code"] for e in report["validation_errors"]]


def test_missing_historical_privacy_rubric_cannot_be_reconstructed_as_gold():
    packet, artifacts, rows = fixture()
    rows[0]["response"]["semantic_guardrails"] = {"privacy_disclosure": {"claims": [{"id": "privacy_disclosure", "status": "pass"}]}}
    packet = build_review_packet(rows, artifacts, versions=VERSIONS)
    privacy = next(s for s in packet["samples"] if s["source"]["group"] == "privacy_disclosure")
    assert "saved_rubric" in privacy["binding_missing"]
    report = evaluate_calibration(packet, [worksheet(packet, "r1"), worksheet(packet, "r2")], artifacts=artifacts)
    assert report["status"] == "invalid"


def test_selection_is_stable_under_input_order_and_keeps_all_six_strata():
    _, artifacts, original = fixture()
    rows, raws = [], {}
    for i in range(12):
        row = deepcopy(original[0])
        row["case_id"] = str(i)
        claim_id = "no_unverified_completion" if i % 2 else "refund_amount"
        row["response"]["rubric"][0]["id"] = claim_id
        row["response"]["rubric_sha256"] = digest(row["response"]["rubric"])
        row["response"]["claims"][0].update(id=claim_id, status=("pass", "fail", "unknown")[i % 3])
        rows.append(row)
        raws[f"{i}-t0"] = artifacts["C1-t0"]
    protocol = {"max_per_stratum": 1, "ordinary_reviews": 1, "high_risk_reviews": 2}
    a = build_review_packet(rows, raws, versions=VERSIONS, protocol=protocol)
    b = build_review_packet(list(reversed(rows)), raws, versions=VERSIONS, protocol=protocol)
    assert a["packet_sha256"] == b["packet_sha256"]
    assert len(a["samples"]) == 6
    assert a["sampling"]["population_claims"] == 12


def test_tampered_packet_digest_is_invalid_even_with_valid_review_labels():
    packet, artifacts, _ = fixture(high_risk=False)
    review = worksheet(packet)
    packet["versions"]["grader"] = "new-grader"
    assert evaluate_calibration(packet, [review], artifacts=artifacts)["status"] == "invalid"


@pytest.mark.parametrize("token", ["-1", "+0", "00", "01", "٠", "０", " 0", "0 ", "1_0", "-"])
def test_review_rejects_non_json_pointer_array_indexes(token):
    packet, artifacts, _ = fixture(high_risk=False)
    review = worksheet(packet)
    review["reviews"][0]["evidence_refs"] = [f"/view/evidence/{token}"]
    report = evaluate_calibration(packet, [review], artifacts=artifacts)
    assert report["status"] == "invalid"
    assert "invalid_evidence_reference" in {error["code"] for error in report["validation_errors"]}


def test_json_pointer_object_keys_allow_zeroes_and_escaped_slashes():
    from billingops.evaluation.v2_calibration import _pointer
    raw = {"01": {"a/b": {"~": ["receipt"]}}}
    assert _pointer(raw, "/01/a~1b/~0/0") == (True, "receipt")
    assert _pointer({"~2": "not a valid escape"}, "/~2") == (False, None)
