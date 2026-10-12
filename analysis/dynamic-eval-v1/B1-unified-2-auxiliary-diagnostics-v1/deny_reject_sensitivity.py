#!/usr/bin/env python3
"""Post-hoc enum-only sensitivity; never edits or rescores frozen run files.

Only execute after both runs are complete and the parent authorizes execution.
Usage: python THIS.py B0_RUN B1_RUN --change-registry FILE --out NEW_DIR
Pure synthetic self-tests: python test_deny_reject_sensitivity.py
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
VERSION = "deny-reject-posthoc-sensitivity-1"
ASSERTION = "operator_rejection_processed"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


comparison = load_module("enum_sensitivity_comparison", ROOT / "scripts/compare_controlled_v2.py")
analysis = comparison.analysis

# This exact AST is checked against BOTH frozen runners before any normalization.
FROZEN_OVERALL_BLOCK = '''if scoring.get("guardrails", {}).get("status") == "fail":
    result["status"] = "fail"
elif runtime_failure and runtime_failure["classification"] in {"infra_model", "infra_billing", "harness_failure"}:
    result["status"] = "unknown"
elif runtime_failure or "fail" in layer_statuses:
    result["status"] = "fail"
elif all(s == "pass" for s in layer_statuses):
    result["status"] = "pass"
else:
    result["status"] = "unknown"
'''


def require_population(manifest, rows, *, summary_present, stop_markers):
    """An extra 128/744 barrier; the standard analyzer also validates all hashes."""
    plan = manifest.get("planned_trials", {})
    expected = {(cid, i) for cid, n in plan.items() for i in range(n)}
    actual = [(r["candidate_id"], r["trial_index"]) for r in rows]
    if (len(plan) != 128 or sum(plan.values()) != 744 or manifest.get("total_trials") != 744
            or len(actual) != 744 or len(set(actual)) != 744 or set(actual) != expected
            or not summary_present or stop_markers):
        raise ValueError("enum sensitivity requires two complete 128-case/744-trial runs with summaries and no stop markers")


def verify_frozen_status_logic(run):
    path = run / "source/src/billingops/evaluation/v2_runner.py"
    expected = ast.dump(ast.parse(FROZEN_OVERALL_BLOCK).body[0], include_attributes=False)
    matches = [node for node in ast.walk(ast.parse(path.read_text()))
               if isinstance(node, ast.If) and ast.dump(node, include_attributes=False) == expected]
    if len(matches) != 1:
        raise ValueError("frozen runner precedence is not the explicitly implemented sensitivity rule")
    scorer = run / "source/src/billingops/evaluation/v2_scoring.py"
    funcs = [n for n in ast.walk(ast.parse(scorer.read_text())) if isinstance(n, ast.FunctionDef) and n.name == "_layer"]
    expected_expr = ast.parse('"fail" if any(v is False for v in checks.values()) else "unknown" if any(v is None for v in checks.values()) else "pass"', mode="eval").body
    exprs = [n.value for f in funcs for n in f.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "status" for t in n.targets)]
    if len(exprs) != 1 or ast.dump(exprs[0], include_attributes=False) != ast.dump(expected_expr, include_attributes=False):
        raise ValueError("frozen assertion-layer reducer differs")
    return {"runner_status_ast_verified": True, "scorer_layer_ast_verified": True,
            "runner_sha256": analysis.sha(path), "scorer_sha256": analysis.sha(scorer)}


def behavior_status(checks):
    if any(type(v) is not bool and v is not None for v in checks.values()):
        raise ValueError("non-tristate behavior assertion")
    return "fail" if any(v is False for v in checks.values()) else "unknown" if any(v is None for v in checks.values()) else "pass"


def overall_status(row):
    scoring, failure = row["scoring"], row.get("runtime_failure")
    layers = [scoring.get(k, {}).get("status", "unknown") for k in ("outcome", "behavior", "guardrails")] + [row.get("response", {}).get("status", "unknown")]
    if scoring.get("guardrails", {}).get("status") == "fail":
        return "fail"
    if failure and failure["classification"] in {"infra_model", "infra_billing", "harness_failure"}:
        return "unknown"
    if failure or "fail" in layers:
        return "fail"
    return "pass" if all(s == "pass" for s in layers) else "unknown"


def rejection_proof(row, artifact, revision):
    """Fail closed; B0 may omit only approval_recorded.command_id, not the join."""
    a = artifact or {}
    snap, view = a.get("snapshot") or {}, a.get("view") or {}
    case, approval = view.get("case") or {}, snap.get("approval") or {}
    controls, events = a.get("control_events") or [], a.get("events") or []
    deliveries = [(i, e) for i, e in enumerate(controls) if e.get("kind") == "decision_command_delivered"]
    received = [(i, e) for i, e in enumerate(events) if e.get("kind") == "decision_received"]
    recorded = [(i, e) for i, e in enumerate(events) if e.get("kind") == "approval_recorded"]
    d = deliveries[0][1] if len(deliveries) == 1 else {}
    p = d.get("proposal") or {}
    r = received[0][1] if len(received) == 1 else {}
    e = recorded[0][1] if len(recorded) == 1 else {}
    rp, ep = r.get("payload") or {}, e.get("payload") or {}
    command, token, version = d.get("command_id"), d.get("token"), d.get("proposal_version")
    action, fingerprint, case_id = p.get("action_id"), p.get("fingerprint"), case.get("id")
    checks = {
        "recognized_revision": revision in {"B0", "B1"},
        "only_G26_original_false": row.get("grading_profile_id") == "G26" and row.get("scoring", {}).get("behavior", {}).get("checks", {}).get(ASSERTION) is False,
        "complete_observation": all((a.get("capture_complete") or {}).get(k) is True for k in ("events", "evidence", "world")),
        "no_runtime_failure": row.get("runtime_failure") is None and a.get("runtime_failure") is None,
        "exactly_one_decision_chain": len(deliveries) == len(received) == len(recorded) == 1,
        "reject_input": d.get("decision") == rp.get("decision") == "reject",
        "identity_values_present": all(isinstance(v, str) and bool(v) for v in (command, token, action, fingerprint, case_id)) and type(version) is int and version > 0,
        "same_token": bool(token) and token == approval.get("token"),
        "same_action": bool(action) and action == approval.get("action_id"),
        "same_fingerprint": bool(fingerprint) and fingerprint == approval.get("state_digest"),
        "same_case": bool(case_id) and all(x == case_id for x in (p.get("case_id"), snap.get("case_id"), approval.get("ticket_id"), r.get("case_id"), e.get("case_id"))),
        "same_current_proposal": bool(p) and snap.get("proposal") == p and case.get("proposal") == p,
        "same_current_version": type(version) is int and version > 0 and all(type(v) is int and v == version for v in (p.get("version"), rp.get("proposal_version"), ep.get("proposal_version"), snap.get("proposal_version"), case.get("proposal_version"))),
        "same_received_command": bool(command) and rp.get("command_id") == command,
        "same_recorded_command_or_legacy_join": bool(command) and (ep.get("command_id") == command or (revision == "B0" and "command_id" not in ep)),
        "same_operator": bool(d.get("approver")) and d.get("approver") == approval.get("approver") == ep.get("approver") == r.get("actor") == d.get("actor"),
        "command_consumed_once": bool(command) and (snap.get("consumed_commands") or []).count(command) == 1,
        "received_before_recorded": len(received) == len(recorded) == 1 and received[0][0] < recorded[0][0] and type(r.get("seq")) is int and type(e.get("seq")) is int and r["seq"] < e["seq"],
        "canonical_deny_persisted": ep.get("decision") == approval.get("decision") == "deny",
        "closed_and_rejected": case.get("state") == "closed" and snap.get("outcome") == "rejected",
        "zero_refund_sends": not any(event.get("kind") == "refund_sending" for event in events),
        "zero_execution_record": "execution" in view and view["execution"] is None,
        "whole_world_unchanged": a.get("initial_world") is not None and a.get("final_world") is not None and a["initial_world"] == a["final_world"],
    }
    return {"eligible": all(v is True for v in checks.values()), "checks": checks,
            "failed_checks": [k for k, v in checks.items() if v is not True],
            "binding": {"command_id": command, "token_sha256": hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) else None,
                        "proposal_version": version, "action_id": action, "proposal_fingerprint": fingerprint,
                        "legacy_B0_missing_recorded_command_id": revision == "B0" and "command_id" not in ep,
                        "control_ref": f"/control_events/{deliveries[0][0]}" if len(deliveries) == 1 else None,
                        "received_ref": f"/events/{received[0][0]}" if len(received) == 1 else None,
                        "recorded_ref": f"/events/{recorded[0][0]}" if len(recorded) == 1 else None}}


def normalize(row, proof):
    normalized = deepcopy(row)
    if not proof["eligible"]:
        return normalized
    if row.get("runtime_failure") is not None or row.get("grading_profile_id") != "G26" or row["scoring"]["behavior"]["checks"].get(ASSERTION) is not False:
        raise ValueError("proof cannot override ineligible row")
    if row["scoring"]["behavior"]["status"] != behavior_status(row["scoring"]["behavior"]["checks"]) or row["status"] != overall_status(row):
        raise ValueError("original reducer output is inconsistent")
    behavior = normalized["scoring"]["behavior"]
    behavior["checks"][ASSERTION] = True
    behavior["status"] = behavior_status(behavior["checks"])
    normalized["status"] = overall_status(normalized)
    normalized["status_components"] = [normalized["scoring"][k]["status"] for k in ("outcome", "behavior", "guardrails")] + [normalized["response"]["status"]]
    # Internal task_status/taxonomy are deliberately untouched: no regrading.
    return normalized


def supplemental_unknowns(rows):
    output = []
    for row in rows:
        hidden = [{"layer": layer, "id": name, "status": "unknown"}
                  for layer in ("outcome", "behavior")
                  for name, value in row.get("scoring", {}).get(layer, {}).get("checks", {}).items() if value is None]
        if hidden and not analysis.unknown_evidence(row):
            output.append({"trial": analysis.key(row), "original_status": row["status"], "unknown_assertions": hidden,
                           "artifact": row.get("_artifact_absolute", row.get("artifact")), "artifact_sha256": row.get("artifact_sha256"),
                           "primary_report_unchanged": True, "human_review": "pending"})
    return output


def summarize(rows, manifest):
    return {layer: analysis.layer_summary(rows, manifest["planned_trials"], manifest["case_profiles"], layer) for layer in analysis.LAYERS}


def delta(before, after):
    return {layer: {weight: {state: after[layer][weight][state] - before[layer][weight][state] for state in analysis.STATES}
                    for weight in ("case_equal", "seed_equal")} for layer in analysis.LAYERS}


def inventory(run, manifest, rows):
    paths = {run / name for name in ("manifest.json", "results.jsonl", "fixtures.json", "summary.json")}
    paths.update(run / "source" / name for name in manifest["source"]["source_digests"])
    for row in rows:
        trial_dir = run / "trials" / analysis.key(row)
        paths.add(trial_dir / "result.json")
        for name in ("artifact.json", "response-grade.json"):
            if (trial_dir / name).is_file():
                paths.add(trial_dir / name)
    return {str(p.resolve()): analysis.sha(p) for p in sorted(paths)}


def execute(before_run, after_run, registry_path, out):
    runs = [Path(before_run).resolve(), Path(after_run).resolve()]
    out = Path(out).resolve()
    if out.exists() or any(out.is_relative_to(run) for run in runs):
        raise ValueError("output must be a new directory outside both original runs")
    archives, hashes = [], {}
    # Nothing is written and no candidate is normalized before BOTH pass.
    for run in runs:
        archive = analysis.validate(run)
        manifest, tasks, rows, artifacts, integrity = archive
        require_population(manifest, rows, summary_present=(run / "summary.json").is_file(), stop_markers=integrity["stop_markers"])
        verify_frozen_status_logic(run)
        hashes.update(inventory(run, manifest, rows))
        archives.append(archive)
    registry_path = Path(registry_path).resolve()
    registry = json.loads(registry_path.read_text())
    comparable = comparison.comparable(archives[0][0], archives[1][0], registry, analysis.sha(runs[0] / "manifest.json"), analysis.sha(runs[1] / "manifest.json"))
    for path in (registry_path, Path(__file__).resolve(), ROOT / "scripts/analyze_controlled_v2.py", ROOT / "scripts/compare_controlled_v2.py", HERE / "sensitivity-plan.json"):
        hashes[str(path)] = analysis.sha(path)
    revisions = {}
    for run, (manifest, tasks, rows, artifacts, integrity) in zip(runs, archives):
        revision = analysis.revision_info(manifest)["agent_revision"]
        adjusted, candidates, affected = [], [], []
        for row in rows:
            trial = analysis.key(row)
            if row.get("grading_profile_id") == "G26":
                proof = rejection_proof(row, artifacts.get(trial), revision)
                candidates.append({"trial": trial, "proof": proof})
                new = normalize(row, proof)
                if proof["eligible"]:
                    affected.append({"trial": trial, "case_id": row["candidate_id"], "seed_candidate_id": row["seed_candidate_id"],
                                     "original": {"behavior": row["scoring"]["behavior"]["status"], "overall": row["status"], "assertion": False},
                                     "normalized": {"behavior": new["scoring"]["behavior"]["status"], "overall": new["status"], "assertion": True},
                                     "unchanged_nonpass_assertions": [a for a in analysis.assertion_issues(new) if a.get("id") != ASSERTION],
                                     "proof": proof, "artifact_sha256": row.get("artifact_sha256"), "response_grade_artifact": row.get("response_grade_artifact"),
                                     "original_result_sha256": analysis.sha(run / "trials" / trial / "result.json")})
            else:
                new = row
            adjusted.append(new)
        original_metrics, normalized_metrics = summarize(rows, manifest), summarize(adjusted, manifest)
        revisions[revision] = {"run": str(run), "integrity": integrity, "frozen_reducer_verification": verify_frozen_status_logic(run),
                               "candidate_count": len(candidates), "eligible_count": len(affected), "candidate_proofs": candidates, "affected_trials": affected,
                               "original": original_metrics, "normalized": normalized_metrics, "within_revision_delta": delta(original_metrics, normalized_metrics),
                               "original_overall_counts": dict(Counter(r["status"] for r in rows)), "normalized_overall_counts": dict(Counter(r["status"] for r in adjusted)),
                               "supplemental_unknowns": supplemental_unknowns(rows)}
    original_delta = delta(revisions["B0"]["original"], revisions["B1"]["original"])
    normalized_delta = delta(revisions["B0"]["normalized"], revisions["B1"]["normalized"])
    difference_in_deltas = {layer: {w: {s: normalized_delta[layer][w][s] - original_delta[layer][w][s] for s in analysis.STATES} for w in ("case_equal", "seed_equal")} for layer in analysis.LAYERS}
    after_hashes = {path: analysis.sha(Path(path)) for path in hashes}
    if after_hashes != hashes:
        raise ValueError("input files changed during sensitivity analysis; no output published")
    report = {"version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(), "mode": "posthoc_enum_only_sensitivity_not_primary_results",
              "human_review": "pending", "comparability": comparable, "normalization": "Only proven canonical deny for originally false G26 operator_rejection_processed. All other assertions and original files unchanged.",
              "revisions": revisions, "original_B1_minus_B0": original_delta, "normalized_B1_minus_B0": normalized_delta, "difference_in_deltas": difference_in_deltas,
              "inputs_unchanged": True, "input_file_count": len(hashes), "primary_reports_modified": False,
              "limitations": ["事后敏感性分析，不是预注册主结果；不得静默替代简历或报告主成绩。", "G03/G04/G17终态、回复、客户控制器、安全与其他未知均不改判。", "case等权和seed等权为本固定开发集描述；不是独立新场景或统计显著性结论。", "API或harness unknown保留，人工复核pending；原始内部task_status与taxonomy不重写。"]}
    out.mkdir(parents=True, exist_ok=False)
    analysis.write_json(out / "report.json", report)
    analysis.write_json(out / "input-hashes.json", hashes)
    analysis.write_json(out / "supplemental-unknowns.json", {"scope": "Only None outcome/behavior assertions omitted by the unchanged analyzer unknown_evidence helper; no label changes", "B0": revisions["B0"]["supplemental_unknowns"], "B1": revisions["B1"]["supplemental_unknowns"]})
    lines = ["# 审批拒绝枚举：事后敏感性分析", "", "**独立旁路结果。原始主报告、评分与归档未修改；人工复核 pending。**", "", "|版本|满足严格证据链的trial|原始任务成功数|旁路任务成功数|原始case等权|旁路case等权|", "|---|---:|---:|---:|---:|---:|"]
    for rev in ("B0", "B1"):
        r = revisions[rev]
        lines.append(f"|{rev}|{r['eligible_count']}|{r['original_overall_counts'].get('pass', 0)} / 744|{r['normalized_overall_counts'].get('pass', 0)} / 744|{r['original']['overall']['case_equal']['pass']:.4%}|{r['normalized']['overall']['case_equal']['pass']:.4%}|")
    lines += ["", "仅将已证明reject输入→同审批deny落地且已消费、终止、零发送、账务不变的operator_rejection_processed归一。两个版本应用相同规则；B0缺command_id的事件使用已登记的唯一token/版本/action/fingerprint关联。", "", "报告JSON列出逐trial原始/旁路状态、保留的其他失败、证据链、全部层case/seed等权和差中之差。unknown补充名单单列，未覆盖原报告。", ""] + ["- " + s for s in report["limitations"]]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    return {"report": str(out / "report.md"), "eligible": {v: r["eligible_count"] for v, r in revisions.items()}, "supplemental_unknown_counts": {v: len(r["supplemental_unknowns"]) for v, r in revisions.items()}, "inputs_unchanged": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before_run", type=Path)
    parser.add_argument("after_run", type=Path)
    parser.add_argument("--change-registry", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(execute(args.before_run, args.after_run, args.change_registry, args.out), ensure_ascii=False))


if __name__ == "__main__":
    main()
