"""Version-bound, offline V2 judge calibration; never manufactures human labels.

The functions validate declarations and immutable evidence bindings, not a
reviewer's identity. An authenticated review system must establish that outside
this module. Rates describe this selected audit sample, not a production rate.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

VERSION = "v2-calibration-1"
LABELS = ("pass", "fail", "unknown")
RISK_ASSERTIONS = {"privacy_disclosure", "no_unverified_completion", "public_amount_accuracy", "no_sensitive_card_request"}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def trial_id(row: Mapping[str, Any]) -> str:
    return f"{row.get('case_id') or row.get('candidate_id') or row.get('task_id')}-t{row.get('trial_index', 0)}"


def _pointer(value: Any, ref: str) -> tuple[bool, Any]:
    try:
        if not isinstance(ref, str) or not ref.startswith("/"):
            return False, None
        for part in ref[1:].split("/"):
            if re.search(r"~(?![01])", part):
                return False, None
            key = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                # RFC 6901 indexes use ASCII decimal, without sign/leading zero.
                # Python int() alone also accepts -1, +1, 01 and Unicode digits.
                if re.fullmatch(r"0|[1-9][0-9]*", key) is None:
                    return False, None
                value = value[int(key)]
            else:
                value = value[key]
        return True, value
    except (KeyError, ValueError, TypeError, IndexError):
        return False, None


def _sealed(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    result = deepcopy(dict(value))
    result[key] = digest(result)
    return result


def build_review_packet(rows: Sequence[Mapping[str, Any]], artifacts: Mapping[str, Mapping[str, Any]], *,
                        versions: Mapping[str, Any], protocol: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Select all claims or a deterministic per-stratum cap without regrading.

    ``artifacts`` uses ``<case_id>-t<trial_index>`` keys. A cap stratifies by
    safety/ordinary and the original pass/fail/unknown verdict. A missing saved
    rubric remains missing; current grader code is never substituted for it.
    """
    protocol = dict(protocol or {"version": "calibration-sampling-1", "max_per_stratum": None,
                                 "high_risk_reviews": 2, "ordinary_reviews": 1})
    cap = protocol.get("max_per_stratum")
    if cap is not None and (type(cap) is not int or cap < 1):
        raise ValueError("max_per_stratum must be a positive integer or null")
    samples, unavailable = [], []
    seen_trials = set()
    for row in rows:
        tid = trial_id(row)
        if tid in seen_trials:
            raise ValueError(f"duplicate trial: {tid}")
        seen_trials.add(tid)
        raw = artifacts.get(tid)
        if raw is None:
            unavailable.append({"trial_id": tid, "reason": "artifact_missing"})
            continue
        grade = row.get("response") or row.get("response_grade") or raw.get("response_grade") or {}
        groups = [("response", grade)]
        for name, guard in (grade.get("semantic_guardrails") or {}).items():
            # This guard points to the identical already-included response claim.
            if name != "tool_result_hallucination":
                groups.append((name, guard))
        if not any(group.get("claims") for _, group in groups):
            unavailable.append({"trial_id": tid, "reason": "atomic_claims_missing"})
        for group_name, group in groups:
            rubric = group.get("rubric") or []
            rules = {r.get("id"): r for r in rubric}
            for index, claim in enumerate(group.get("claims") or []):
                claim_id = str(claim.get("id", ""))
                parent_id = claim.get("parent_assertion_id") or claim_id.split(":/", 1)[0]
                rule = rules.get(claim_id) or rules.get(parent_id)
                boundary = claim.get("temporal_scope") or {}
                message_ref = boundary.get("message_ref")
                exists, message = _pointer(raw, message_ref) if message_ref else (False, None)
                reply = (str(message.get("text") or message.get("content") or "") if isinstance(message, Mapping)
                         else str(message) if exists else str(raw.get("reply") or raw.get("final_message") or ""))
                if rule and rule.get("scope") == "all_customer_visible":
                    reply = json.dumps(raw.get("public_dialogue") or raw.get("observed_messages") or [], ensure_ascii=False)
                observed = []
                for observation in group.get("evidence_index") or grade.get("evidence_index") or []:
                    ref = observation.get("ref", "")
                    found, value = _pointer(raw, ref)
                    observed.append({"ref": ref, "resolved": found, "value": value if found else None})
                missing = []
                if not row.get("artifact_sha256"):
                    missing.append("archived_artifact_sha256")
                if not rule:
                    missing.append("saved_rubric")
                if not group.get("rubric_sha256") or group.get("rubric_sha256") != digest(rubric):
                    missing.append("valid_saved_rubric_sha256")
                judge_version = group.get("version") or grade.get("version")
                if not judge_version:
                    missing.append("judge_version")
                if not (group.get("model") or grade.get("model")):
                    missing.append("judge_model")
                if message_ref and not exists:
                    missing.append("message_ref")
                source = {"trial_id": tid, "group": group_name, "claim_index": index, "claim_id": claim_id,
                          "artifact_path": row.get("artifact"), "artifact_sha256": row.get("artifact_sha256"),
                          "artifact_content_sha256": digest(raw), "response_grade_sha256": digest(grade),
                          "response_grade_artifact": row.get("response_grade_artifact"),
                          "rubric_sha256": group.get("rubric_sha256"), "judge_version": judge_version,
                          "judge_model": group.get("model") or grade.get("model"), "version_bundle_sha256": digest(versions),
                          "reply_sha256": digest(reply)}
                risk = group_name != "response" or parent_id in RISK_ASSERTIONS
                label = claim.get("status") if claim.get("status") in LABELS else "unknown"
                sample = {"sample_id": "cal-" + digest(source)[:24], "source": source,
                          "stratum": f"{'safety' if risk else 'ordinary'}/{label}", "high_risk": risk,
                          "binding_missing": missing, "judge_label": label,
                          "review_material": {"reply": reply, "rubric": rule, "temporal_scope": boundary,
                                              "observations": observed, "artifact_path": row.get("artifact"),
                                              "artifact_sha256": row.get("artifact_sha256"),
                                              "instruction": "Review the complete referenced trace and the stated message-time boundary; later receipts cannot support an earlier claim."}}
                samples.append(_sealed(sample, "sample_sha256"))
    candidates = Counter(s["stratum"] for s in samples)
    selected = []
    for stratum in sorted(candidates):
        members = sorted((s for s in samples if s["stratum"] == stratum), key=lambda s: s["sample_id"])
        selected.extend(members if cap is None else members[:cap])
    return _sealed({"version": VERSION, "versions": dict(versions), "protocol": protocol,
                    "sampling": {"method": "deterministic_hash_within_safety_and_judge_label_strata",
                                 "population_claims": len(samples), "selected_claims": len(selected),
                                 "population_by_stratum": dict(candidates), "selected_by_stratum": dict(Counter(s["stratum"] for s in selected)),
                                 "interpretation": "selected audit sample; not a population error-rate estimate"},
                    "unavailable_trials": unavailable, "samples": selected,
                    "identity_assurance": "self-declarations only; no authentication or human identity verification"}, "packet_sha256")


def review_template(packet: Mapping[str, Any]) -> dict[str, Any]:
    """A blind review worksheet: original judge labels/reasons are excluded."""
    return {"version": VERSION, "packet_sha256": packet["packet_sha256"],
            "reviewer": {"reviewer_id": "", "domain_expert": False, "independent_of_agent_and_grader": False,
                         "blind_to_judge_labels": False, "attestation_reference": ""},
            "reviews": [{"sample_id": s["sample_id"], "sample_sha256": s["sample_sha256"],
                         "label": None, "reason": "", "evidence_refs": [], "review_material": s["review_material"]}
                        for s in packet["samples"]],
            "instructions": "Each reviewer fills a separate copy. Safety claims need two distinct independent reviewers; disagreements remain unknown. Do not infer labels from empty fields."}


def _wilson(yes: int, n: int) -> dict[str, Any]:
    if not n:
        return {"estimate": None, "lower": None, "upper": None, "n": 0, "level": .95, "method": "wilson"}
    z = 1.959963984540054
    p = yes / n
    center = (p + z*z/(2*n))/(1+z*z/n)
    width = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
    return {"estimate": p, "lower": max(0., center-width), "upper": min(1., center+width),
            "n": n, "level": .95, "method": "wilson", "assumption": "independent audited items; clustered/shared trials can invalidate this interval"}


def _statistics(pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    matrix = {human: {judge: 0 for judge in LABELS} for human in LABELS}
    for judge, human in pairs:
        matrix[human][judge] += 1
    determined = [(j, h) for j, h in pairs if j != "unknown" and h != "unknown"]
    tp = sum(j == h == "fail" for j, h in determined)
    predicted = sum(j == "fail" for j, h in determined)
    actual = sum(h == "fail" for j, h in determined)
    n = len(pairs)
    agreement = sum(j == h for j, h in pairs)
    expected = sum(sum(matrix[l].values()) * sum(matrix[h][l] for h in LABELS) for l in LABELS) / (n*n) if n else None
    kappa = ((agreement/n - expected)/(1-expected)) if n and expected < 1 else None
    return {"confusion_matrix": matrix, "matrix_axes": "rows=human, columns=judge",
            "pairs": n, "determinate_pairs": len(determined), "judge_unknown": sum(j == "unknown" for j, h in pairs),
            "human_unknown": sum(h == "unknown" for j, h in pairs), "agreement": _wilson(agreement, n),
            "failure_precision": _wilson(tp, predicted),
            # Abstention on a human failure is not credited as detecting it.
            "failure_recall": _wilson(sum(j == h == "fail" for j, h in pairs), sum(h == "fail" for j, h in pairs)),
            "cohen_kappa": kappa}


def evaluate_calibration(packet: Mapping[str, Any], reviews: Sequence[Mapping[str, Any]], *,
                         artifacts: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Validate completed worksheets; absent/conflicting review stays unknown.

    Review labels may be pass/fail/unknown. ``None`` is an unfinished template,
    not a human unknown label. Evidence refs must be real JSON pointers into
    the bound raw artifact (or its saved observation list if raw isn't supplied).
    """
    errors = []
    if packet.get("packet_sha256") != digest({k: v for k, v in packet.items() if k != "packet_sha256"}):
        errors.append({"code": "packet_digest_mismatch"})
    samples = {s["sample_id"]: s for s in packet.get("samples", [])}
    if len(samples) != len(packet.get("samples", [])):
        errors.append({"code": "duplicate_sample_id"})
    accepted: dict[str, list[dict[str, Any]]] = defaultdict(list)
    declared_reviewers = {}
    for worksheet in reviews:
        reviewer = worksheet.get("reviewer") or {}
        rid = str(reviewer.get("reviewer_id") or "").strip()
        valid_reviewer = (bool(rid) and reviewer.get("domain_expert") is True
                          and reviewer.get("independent_of_agent_and_grader") is True
                          and reviewer.get("blind_to_judge_labels") is True
                          and bool(reviewer.get("attestation_reference")))
        if worksheet.get("packet_sha256") != packet.get("packet_sha256") or not valid_reviewer:
            errors.append({"code": "invalid_packet_or_reviewer_declaration", "reviewer_id": rid})
            continue
        declared_reviewers[rid] = reviewer
        for review in worksheet.get("reviews", []):
            sid = review.get("sample_id")
            sample = samples.get(sid)
            if review.get("label") is None:
                continue
            error = None
            if sample is None:
                error = "unknown_sample"
            elif sample.get("sample_sha256") != digest({k: v for k, v in sample.items() if k != "sample_sha256"}):
                error = "sample_digest_mismatch"
            elif review.get("sample_sha256") != sample.get("sample_sha256"):
                error = "review_sample_digest_mismatch"
            elif sample.get("binding_missing"):
                error = "source_binding_missing"
            elif review.get("label") not in LABELS or not str(review.get("reason") or "").strip():
                error = "invalid_label_or_missing_reason"
            elif any(r["reviewer_id"] == rid for r in accepted[sid]):
                error = "duplicate_reviewer_for_sample"
            else:
                raw = (artifacts or {}).get(sample["source"]["trial_id"])
                if artifacts is not None and (raw is None or digest(raw) != sample["source"]["artifact_content_sha256"]):
                    error = "artifact_digest_mismatch"
                refs = review.get("evidence_refs")
                allowed = {o["ref"] for o in sample["review_material"]["observations"] if o["resolved"]}
                if not isinstance(refs, list) or any(not isinstance(ref, str) or not (_pointer(raw, ref)[0] if raw is not None else ref in allowed) for ref in refs):
                    error = "invalid_evidence_reference"
                rule = sample["review_material"].get("rubric") or {}
                if review.get("label") in {"pass", "fail"} and rule.get("requires_observation", True) and not refs:
                    error = "required_evidence_reference_missing"
            if error:
                errors.append({"code": error, "sample_id": sid, "reviewer_id": rid})
                continue
            accepted[sid].append({"reviewer_id": rid, "label": review["label"], "reason": review["reason"],
                                  "evidence_refs": list(review["evidence_refs"])})
    decisions, pairs, strata = [], [], defaultdict(list)
    for sid, sample in samples.items():
        evidence = accepted[sid]
        required = max(2 if sample["high_risk"] else 1, int(packet["protocol"].get("high_risk_reviews" if sample["high_risk"] else "ordinary_reviews", 1)))
        labels = {r["label"] for r in evidence}
        state = "pending" if len(evidence) < required else "disagreement" if len(labels) != 1 else "reviewed"
        human = next(iter(labels)) if state == "reviewed" else None
        decisions.append({"sample_id": sid, "status": state, "human_label": human, "reviews": evidence, "required_reviews": required})
        if human is not None:
            pair = (sample["judge_label"], human)
            pairs.append(pair)
            strata[sample["stratum"]].append(pair)
    counts = Counter(d["status"] for d in decisions)
    status = "invalid" if errors else "pending" if not samples or counts["pending"] or counts["disagreement"] or packet.get("unavailable_trials") else "complete"
    return _sealed({"version": VERSION, "packet_sha256": packet.get("packet_sha256"),
                    "versions": packet.get("versions", {}), "status": status,
                    "coverage": {"selected": len(samples), "reviewed": counts["reviewed"], "pending": counts["pending"],
                                 "disagreement": counts["disagreement"], "binding_missing": sum(bool(s.get("binding_missing")) for s in samples.values()),
                                 "unavailable_trials": len(packet.get("unavailable_trials", [])),
                                 "reviewed_fraction": counts["reviewed"] / len(samples) if samples else None},
                    "statistics": _statistics(pairs), "by_stratum": {name: _statistics(value) for name, value in strata.items()},
                    "decisions": decisions, "validation_errors": errors, "reviewers": list(declared_reviewers.values()),
                    "identity_assurance": "self-declared independent experts; not authenticated by this module",
                    "interpretation": "sample calibration, not task success or production safety certification"}, "report_sha256")
