#!/usr/bin/env python3
"""Read-only B0/B1 measurement review, with no model calls or primary-score edits.

Deterministic outcome/behaviour checks are recomputed under one code version.
Existing semantic labels are explicitly retained as historical inputs only;
new public-dialogue checks and message-time progress judgements remain pending.
No new overall success rate is calculated from this mixed, incomplete review.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from billingops.evaluation import v2_response, v2_scoring

VERSION = "measurement-v1.2-offline-review-1"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review(report_paths: dict[str, Path], output: Path) -> dict:
    output = output.resolve()
    if output.exists():
        raise ValueError("Output must be a new directory; original reports are immutable")
    hashes: dict[str, str] = {}

    def read(path: Path):
        path = path.resolve()
        value = path.read_bytes()
        observed = hashlib.sha256(value).hexdigest()
        if str(path) in hashes and hashes[str(path)] != observed:
            raise ValueError(f"Input changed while reading: {path}")
        hashes[str(path)] = observed
        return json.loads(value)

    reports = {version: read(path) for version, path in report_paths.items()}
    if set(reports) != {"B0", "B1"}:
        raise ValueError("Both B0 and B1 are required for the same measurement rule")
    populations = {version: [(r["case_id"], r["trial"]) for r in report["per_trial"]] for version, report in reports.items()}
    if any(len(rows) != 744 or len(set(rows)) != 744 for rows in populations.values()) or set(populations["B0"]) != set(populations["B1"]):
        raise ValueError("Require the exact matching 744 logical trial slots, including registered B1 replacements")
    revisions = {}
    for version, report in reports.items():
        actual_revision = report.get("agent_revision")
        if actual_revision is None and report.get("run"):
            actual_revision = read(Path(report["run"]) / "manifest.json").get("agent_revision")
            if actual_revision is None and report.get("variant") == "B0-v5.4-common-policy-v2":
                actual_revision = "B0"  # Legacy manifest explicitly identifies this frozen variant.
        if actual_revision != version:
            raise ValueError("Report Agent revision differs from the requested label")
        rows = []
        for summary in report["per_trial"]:
            artifact_path = Path(summary["artifact"]).resolve()
            run_root = artifact_path.parents[2]
            if output.is_relative_to(run_root) or output.is_relative_to(report_paths[version].resolve().parent):
                raise ValueError("Output cannot live under an original run or report")
            artifact = read(artifact_path)
            result = read(artifact_path.with_name("result.json"))
            response_file = read(artifact_path.with_name("response-grade.json"))
            if result["artifact_sha256"] != hashes[str(artifact_path)] or response_file["raw_artifact_sha256"] != hashes[str(artifact_path)]:
                raise ValueError(f"Archived artifact hash mismatch: {artifact_path}")
            if result["response"] != response_file["response_grade"] or summary["status"] != result["status"]:
                raise ValueError(f"Summary/archived result disagreement: {artifact_path}")
            task = deepcopy(artifact["task"])
            task["candidate_id"] = task.get("grading_profile_id", task["candidate_id"])
            artifact["response_grade"] = result["response"]
            scoring = v2_scoring.score_trial(task, artifact)
            changes = []
            for layer in ("outcome", "behavior", "guardrails"):
                before = result["scoring"][layer].get("checks", {})
                after = scoring[layer].get("checks", {})
                changes += [{"layer": layer, "assertion": key, "old": before.get(key), "new": after.get(key)}
                            for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]
            new_rules = [rule for rule in v2_response.build_rubric(task) if rule.get("scope") in {"progress", "each_customer_visible"}]
            semantic_plan = []
            for rule, reply, observations, boundary in v2_response._assertion_inputs(artifact, new_rules, v2_response.observed_evidence(artifact)):
                semantic_plan.append({"assertion": rule["id"], "parent_assertion": rule.get("parent_assertion_id", rule["id"]),
                                      "status": "unknown", "evaluation_state": "not_regraded", "message_present": bool(reply),
                                      "temporal_scope": boundary, "observation_refs": [o["ref"] for o in observations]})
            rows.append({"trial": summary["trial"], "case_id": summary["case_id"], "source_origin": summary.get("source_origin", "base"),
                         "artifact": str(artifact_path), "artifact_sha256": hashes[str(artifact_path)],
                         "old_overall_status_unchanged": result["status"], "old_scorer_version": result["scoring"].get("scorer_version"),
                         "new_scorer_version": scoring["scorer_version"], "change_type": "measurement_not_agent_reexecution",
                         "old_response_version": result["response"].get("version"), "new_response_version": v2_response.VERSION,
                         "semantic_label_reuse": "historical_inputs_only_not_new_measurement_pass",
                         "assertion_changes": changes,
                         "old_deterministic_layers": {k: result["scoring"][k] for k in ("outcome", "behavior", "guardrails")},
                         "new_deterministic_layers": {k: scoring[k] for k in ("outcome", "behavior", "guardrails")},
                         "semantic_regrade_plan": semantic_plan, "new_full_measurement_status": "not_completed"})
        revisions[version] = {"source_report": str(report_paths[version].resolve()), "logical_trials": len(rows),
                              "changed_trial_count": sum(bool(r["assertion_changes"]) for r in rows),
                              "changed_assertion_counts": dict(Counter(c["layer"] + ":" + c["assertion"] for r in rows for c in r["assertion_changes"])),
                              "historical_status_counts": dict(Counter(r["old_overall_status_unchanged"] for r in rows)),
                              "new_deterministic_layer_status_counts": {layer: dict(Counter(r["new_deterministic_layers"][layer]["status"] for r in rows)) for layer in ("outcome", "behavior", "guardrails")},
                              "semantic_judgements_pending": sum(len(r["semantic_regrade_plan"]) for r in rows), "trials": rows}
    for path in (Path(__file__), Path(v2_scoring.__file__), Path(v2_response.__file__)):
        hashes[str(path.resolve())] = sha(path)
    if any(sha(Path(path)) != expected for path, expected in hashes.items()):
        raise ValueError("Inputs changed during review; no output published")
    result = {"version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
              "loop": "evaluation_system", "changes": ["strict_rejection_event_join", "grounded_readonly_terminal_mapping", "message_time_evidence", "public_dialogue_amount_coverage"],
              "old_versions": {"scorer": "business-v2-trace-1.1", "response": "business-v2-response-1.1"},
              "new_versions": {"scorer": v2_scoring.SCORER_VERSION, "response": v2_response.VERSION},
              "api_calls": 0, "original_files_unchanged": True, "new_overall_success_rate": None,
              "why_no_new_success_rate": "New public-message amount checks and corrected temporal semantic judgements require fresh calibrated review; reusing final-only labels would be incomplete.",
              "human_review": "pending", "revisions": revisions}
    output.mkdir(parents=True, exist_ok=False)
    (output / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (output / "input-hashes.json").write_text(json.dumps(hashes, ensure_ascii=False, indent=2) + "\n")
    lines = ["# 测量层 1.2：B0/B1 同口径离线复核", "", "这是评测系统 Loop 的修复影响核查，没有重新执行 Agent，没有调用模型，也没有覆盖原始成绩。新增全对话金额判断及按消息时点判断进度仍待重评，因此不计算新版任务成功率。", "", "|版本|逻辑试验|确定性断言有变化的试验|待语义重评项|", "|---|---:|---:|---:|"]
    for version, row in revisions.items():
        lines.append(f"|{version}|{row['logical_trials']}|{row['changed_trial_count']}|{row['semantic_judgements_pending']}|")
    lines += ["", "拒绝处理必须有唯一命令、版本、token、action、指纹、消费与零写入证据链；B0缺少记录事件command_id时仍检查其余完整关联。只读终态映射必须满足实际查询证据、未变账务、正确必要回复和无提案/执行。JSON保存逐条旧/新断言、证据检查以及未重评项目。", "", "只读映射使用已有必要回复标签作为历史输入；这些标签不代表全对话新规则已通过。所有变化是测量修复，不能归因于B2 Agent能力提升。原始文件哈希在input-hashes.json中保留并已验证。", ""]
    (output / "report.md").write_text("\n".join(lines))
    return {"report": str(output / "report.md"), "original_files_unchanged": True,
            "changed_trials": {k: v["changed_trial_count"] for k, v in revisions.items()},
            "pending_semantic_assertions": {k: v["semantic_judgements_pending"] for k, v in revisions.items()},
            "new_overall_success_rate": None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--b0-report", required=True, type=Path)
    parser.add_argument("--b1-report", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(review({"B0": args.b0_report, "B1": args.b1_report}, args.out), ensure_ascii=False))


if __name__ == "__main__":
    main()
