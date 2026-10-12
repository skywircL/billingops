#!/usr/bin/env python3
"""Compare two complete frozen 128-case/744-trial B0/B1 reports, without regrading.

Usage: python scripts/compare_controlled_v2.py B0_REPORT_DIR B1_REPORT_DIR
       --change-registry reviewed-changes.json --out NEW_DIR

The change registry lists exact before/after manifest values and source hashes.
It documents implementation changes; it cannot waive fixed task/grader controls.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import statistics

_spec = importlib.util.spec_from_file_location("comparison_analysis", Path(__file__).with_name("analyze_controlled_v2.py"))
analysis = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(analysis)

VERSION = "controlled-v2-comparison-1"
REGISTRY_VERSION = "controlled-v2-comparison-changes-1"
MANIFEST_CHANGE_FIELDS = {"agent_revision", "variant", "runner_version", "purpose", "baseline_policy_note"}
IGNORED_MANIFEST_FIELDS = {"registered_at", "plan_sha256", "source"}
SOURCE_METADATA_CHANGE_FIELDS = {"tool_schemas_sha256"}
MISSING = {"__absent__": True}


def changed_fields(before: dict, after: dict, *, ignored=frozenset()) -> dict:
    return {k: {"before": before.get(k, MISSING), "after": after.get(k, MISSING)}
            for k in sorted(set(before) | set(after))
            if k not in ignored and before.get(k, MISSING) != after.get(k, MISSING)}


def source_category(path: str) -> str | None:
    if path == "src/billingops/evaluation/v2_runner.py":
        return "runner_revision_switch"
    if path == "data/eval-design/dynamic-v1/protocol.yaml":
        return "operational_metadata"
    if path.startswith("src/billing_service/") and path.endswith(".py"):
        return "billing_implementation"
    if (path.startswith("src/billingops/") and path.endswith(".py")
            and not path.startswith(("src/billingops/evaluation/", "src/billingops/observability/"))):
        return "agent_implementation"
    return None


def _check_registry(actual: dict, records: list, *, name: str, category=False) -> None:
    entries = {r.get("path"): r for r in records}
    if len(entries) != len(records) or set(entries) != set(actual):
        raise ValueError(f"{name} registry must exactly enumerate all and only changed paths")
    for path, values in actual.items():
        entry = entries[path]
        if entry.get("before") != values["before"] or entry.get("after") != values["after"]:
            raise ValueError(f"{name} registered before/after mismatch: {path}")
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            raise ValueError(f"{name} needs an explicit change reason: {path}")
        if category and (source_category(path) is None or entry.get("category") != source_category(path)):
            raise ValueError(f"source change cannot be waived or has the wrong category: {path}")


def comparable(before: dict, after: dict, registry: dict, before_hash: str, after_hash: str) -> dict:
    """Fail closed on any unregistered change or changed evaluation conditions."""
    if registry.get("schema_version") != REGISTRY_VERSION:
        raise ValueError("unrecognized explicit change registry schema")
    if registry.get("before_manifest_sha256") != before_hash or registry.get("after_manifest_sha256") != after_hash:
        raise ValueError("change registry must bind both exact frozen manifests")
    if analysis.revision_info(before)["agent_revision"] != "B0" or analysis.revision_info(after)["agent_revision"] != "B1":
        raise ValueError("comparison requires B0 before and B1 after")
    for manifest in (before, after):
        if (manifest.get("mode") != "unified" or len(manifest.get("candidate_ids", [])) != 128
                or len(set(manifest["candidate_ids"])) != 128 or manifest.get("total_trials") != 744
                or sum(manifest.get("planned_trials", {}).values()) != 744):
            raise ValueError("comparison requires the same complete 128-case/744-trial protocol")
    required = {"fixture_sha256", "case_profiles", "planned_trials", "risk_assignment", "cohorts",
                "model", "policy_context", "evaluation_versions", "runtime", "deadline_s", "max_rounds",
                "seed", "workers", "estimated_usd_per_million_tokens"}
    for field in required:
        if field not in before or field not in after or before[field] != after[field]:
            raise ValueError(f"fixed evaluation condition differs or is missing: {field}")
    manifest_changes = changed_fields(before, after, ignored=IGNORED_MANIFEST_FIELDS)
    if set(manifest_changes) - MANIFEST_CHANGE_FIELDS:
        raise ValueError("non-implementation manifest change: " + ",".join(sorted(set(manifest_changes) - MANIFEST_CHANGE_FIELDS)))
    if after.get("runner_version") != "v2-runner-1.4" or before.get("runner_version") != "v2-runner-1.3":
        raise ValueError("this registered comparison supports only the v1.3 to v1.4 revision-switch runner transition")
    _check_registry(manifest_changes, registry.get("manifest_changes", []), name="manifest")
    source_before, source_after = before.get("source", {}), after.get("source", {})
    metadata = changed_fields(source_before, source_after, ignored={"captured_at", "source_digests"})
    if set(metadata) - SOURCE_METADATA_CHANGE_FIELDS:
        raise ValueError("fixed source metadata differs: " + ",".join(sorted(set(metadata) - SOURCE_METADATA_CHANGE_FIELDS)))
    _check_registry(metadata, registry.get("source_metadata_changes", []), name="source metadata")
    sources = changed_fields(source_before.get("source_digests", {}), source_after.get("source_digests", {}))
    _check_registry(sources, registry.get("source_changes", []), name="source", category=True)
    return {"status": "same_frozen_evaluation_conditions_with_declared_implementation_changes",
            "fixture_sha256": before["fixture_sha256"], "case_count": 128, "planned_trials_per_revision": 744,
            "manifest_changes": registry["manifest_changes"], "source_changes": registry["source_changes"],
            "source_metadata_changes": registry.get("source_metadata_changes", []),
            "interpretation": "同题同评分的工程修复与能力适配比较；Agent、执行器及Billing实现共同改变，不归因为模型训练或仅prompt优化。登记不冒充独立人工审核。"}


def load_report(directory: Path) -> dict:
    directory = directory.resolve()
    report_path = directory / "report.json"
    report = json.loads(report_path.read_text())
    run = Path(report["run"]).resolve()
    manifest, tasks, rows, artifacts, integrity = analysis.validate(run)
    if report.get("case_count") != 128 or report.get("seed_count") != 32 or report.get("hidden_count") != 0:
        raise ValueError("report population is not the registered 128-case development suite")
    for name in ("manifest_sha256", "results_sha256", "fixture_sha256", "complete_plan", "recorded_trials", "planned_trials"):
        if report.get("integrity", {}).get(name) != integrity[name]:
            raise ValueError("report no longer matches frozen archive: " + name)
    if integrity["recorded_trials"] != 744 or not report.get("integrity", {}).get("ok"):
        raise ValueError("comparison refuses partial or invalid reports")
    revision = analysis.revision_info(manifest)
    if report.get("variant") != manifest["variant"] or report.get("mode") != revision["mode"]:
        raise ValueError("report revision is inconsistent with frozen manifest")
    source = directory / "analyzer-source.py"
    if not source.is_file() or analysis.sha(source) != report.get("analysis_program_sha256"):
        raise ValueError("report analyzer source is missing or has changed")
    # Aggregation is checked against recorded verdicts, never a current grader.
    for layer in analysis.LAYERS:
        expected = analysis.layer_summary(rows, manifest["planned_trials"], manifest["case_profiles"], layer)
        if report.get("metrics", {}).get(layer) != expected:
            raise ValueError("report aggregate differs from frozen recorded scores: " + layer)
    return {"directory": directory, "report": report, "run": run, "manifest": manifest,
            "tasks": tasks, "rows": rows, "artifacts": artifacts, "integrity": integrity,
            "report_sha256": analysis.sha(report_path)}


def metrics(data: dict, ids: list[str]) -> dict:
    manifest = data["manifest"]
    rows = [r for r in data["rows"] if r["candidate_id"] in ids]
    planned = {cid: manifest["planned_trials"][cid] for cid in ids}
    return {layer: analysis.layer_summary(rows, planned, manifest["case_profiles"], layer) for layer in analysis.LAYERS}


def delta(old, new):
    return new - old if old is not None and new is not None else None


def metric_comparison(before: dict, after: dict, ids: list[str]) -> dict:
    old, new = metrics(before, ids), metrics(after, ids)
    def compact(metric):
        return {k: metric[k] for k in ("case_count", "parent_seed_count", "planned_trials", "case_equal",
                                        "seed_equal", "case_equal_pass_bounds", "seed_equal_pass_bounds", "trial_counts")}
    return {layer: {"before": compact(old[layer]), "after": compact(new[layer]),
                    "delta_percentage_points": {weight: {s: 100 * delta(old[layer][weight][s], new[layer][weight][s])
                                                             if delta(old[layer][weight][s], new[layer][weight][s]) is not None else None
                                                             for s in analysis.STATES}
                                                for weight in ("case_equal", "seed_equal")}}
            for layer in analysis.LAYERS}


def taxonomy_comparison(before: dict, after: dict) -> dict:
    labels = sorted({label for d in (before, after) for row in d["rows"]
                     for label in row.get("scoring", {}).get("failure_taxonomy", [])})
    def details(data, label):
        selected = [r for r in data["rows"] if label in r.get("scoring", {}).get("failure_taxonomy", [])]
        counts = Counter(r["candidate_id"] for r in selected)
        return {"trial_count": len(selected), "case_equal_fraction": statistics.mean(counts[cid] / n for cid, n in data["manifest"]["planned_trials"].items()),
                "by_overall_status": {s: sum(r["status"] == s for r in selected) for s in analysis.STATES}}
    records = []
    for label in labels:
        old, new = details(before, label), details(after, label)
        records.append({"label": label, "before": old, "after": new, "delta_trial_count": new["trial_count"] - old["trial_count"],
                        "delta_case_equal_percentage_points": 100 * (new["case_equal_fraction"] - old["case_equal_fraction"])})
    return {"human_review": "pending", "labels": records,
            "interpretation": "自动多标签候选，不是人工确认根因；同一trial可重复计数。unknown标签单列，不将基础设施缺证据混作确定Agent缺陷。"}


def _safety(data: dict) -> dict:
    guards = analysis.guardrail_summary(data["rows"], data["artifacts"])
    return {name: {k: v for k, v in values.items() if k not in {"events", "opportunities"}}
            for name, values in guards.items()}


def compare(before_dir: Path, after_dir: Path, registry_path: Path, out: Path) -> dict:
    before, after = load_report(before_dir), load_report(after_dir)
    registry = json.loads(registry_path.read_text())
    contract = comparable(before["manifest"], after["manifest"], registry,
                          before["integrity"]["manifest_sha256"], after["integrity"]["manifest_sha256"])
    out = out.resolve()
    if any(out == d[p] or out.is_relative_to(d[p]) for d in (before, after) for p in ("run", "directory")):
        raise ValueError("comparison output must be outside both immutable archives and reports")
    out.mkdir(parents=True, exist_ok=False)
    ids, profiles = before["manifest"]["candidate_ids"], before["manifest"]["case_profiles"]
    batch = [cid for cid in ids if len(before["tasks"][cid].get("expected_refunds", [])) > 1]
    cohorts = {"all_128": ids,
               "original_32": [cid for cid in ids if profiles[cid]["operator"] == "NONE"],
               "mutations_96": [cid for cid in ids if profiles[cid]["operator"] != "NONE"],
               "benign": before["manifest"]["cohorts"]["benign"],
               "batch_new_capability": batch,
               "excluding_batch": [cid for cid in ids if cid not in batch]}
    comparisons = {name: metric_comparison(before, after, selected) for name, selected in cohorts.items()}
    safeguards = {"before": _safety(before), "after": _safety(after), "combined_with_optimization": False,
                  "release_gate": "not_assessed", "human_review": "pending",
                  "note": "安全事件、未知和实际操作机会分别比较；零已见事件不是零风险证明，不用加权总分批准发布。"}
    false_refusal = {label: analysis.false_refusal(data["rows"], data["artifacts"], data["manifest"])
                     for label, data in (("before", before), ("after", after))}
    exposure = {}
    for label, data in (("before", before), ("after", after)):
        attacks = analysis.attack_summary(data["rows"], data["tasks"], data["artifacts"], data["manifest"])
        faults = analysis.fault_summary(data["rows"], data["tasks"], data["artifacts"])
        exposure[label] = {"attacks": {k: v for k, v in attacks.items() if k != "trials"},
                           "faults": {k: v for k, v in faults.items() if k != "trials"}}
    resources = {}
    unknown = {}
    for label, data in (("before", before), ("after", after)):
        costs, latency, infra = analysis.costs_and_latency(data["rows"])
        resources[label] = {"costs": costs, "latency": latency, "runtime_failures": infra}
        unknown[label] = analysis.response_unknowns(data["rows"])
    overall = comparisons["all_128"]["overall"]
    resume = (f"构建32个人工待审核模拟业务种子及96条受控变体，固定128题、每版本744次DeepSeek试验；"
              f"工程修复与能力补齐后，任务等权全层通过率由{overall['before']['case_equal']['pass']:.2%}变为"
              f"{overall['after']['case_equal']['pass']:.2%}（{overall['delta_percentage_points']['case_equal']['pass']:+.2f}个百分点），"
              "分层核验结果、行为与必要回复，并独立追踪安全事件及未知结果。")
    limitations = ["开发集已曝光，Gold人工逐题签署与新裁判独立校准未完成，Hidden=0；不能写成线上业务效果或独立泛化结论。",
                   "同题均值差为描述性工程版本比较；重复编号不代表随机性配对，不做独立744样本显著性检验。",
                   "128个case共享32个父种子和12个根族；同时列case等权与父种子等权，不能当作128个独立业务目标。",
                   "Agent、状态处理、执行器和Billing原子约束共同变化；新增批量退款能力单列，不归因于单一算法、prompt或模型训练。",
                   "未触发故障/未触达攻击不能算恢复或安全成功；unknown保持在计划分母，误拒只报告待审核候选。",
                   "使用冻结逐trial评分，只做聚合与完整性校验，没有导入当前业务/评分器或重新调用模型。",
                   "简历文本为待人工审阅的事实草稿；安全门禁需独立阈值和审核，本比较不批准发布。"]
    report = {"version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
              "mode": "complete_same_fixture_exploratory_B0_B1", "human_review": "pending", "hidden_count": 0,
              "inputs": {label: {"report": str(data["directory"] / "report.json"), "report_sha256": data["report_sha256"],
                                   "run": str(data["run"]), "manifest_sha256": data["integrity"]["manifest_sha256"],
                                   "results_sha256": data["integrity"]["results_sha256"]}
                         for label, data in (("before", before), ("after", after))},
              "comparability": contract, "cohort_members": cohorts, "metrics": comparisons,
              "batch_cohort_rule": "Frozen expected_refunds has more than one item; outcome and all-layer results reported separately as capability addition.",
              "failure_taxonomy": taxonomy_comparison(before, after), "guardrails": safeguards,
              "false_refusal_candidates": false_refusal, "exposure": exposure, "response_unknowns": unknown,
              "resources": resources, "resume_draft": resume, "limitations": limitations,
              "change_registry_sha256": analysis.sha(registry_path), "comparison_program_sha256": analysis.sha(Path(__file__))}
    analysis.write_json(out / "comparison.json", report)
    analysis.write_json(out / "registered-changes.json", registry)
    (out / "comparator-source.py").write_text(Path(__file__).read_text(), encoding="utf-8")
    lines = ["# 动态Agent B0/B1 同条件版本比较", "", "完整128题、每版本744次；人工复核待完成，Hidden=0。安全指标独立，不批准发布。", "",
             "| 分层 | B0 Case等权通过 | B1 Case等权通过 | 差值（百分点） | B0/B1未知 |", "|---|---:|---:|---:|---:|"]
    for layer, values in comparisons["all_128"].items():
        old, new = values["before"]["case_equal"], values["after"]["case_equal"]
        lines.append(f"| {layer} | {old['pass']:.2%} | {new['pass']:.2%} | {values['delta_percentage_points']['case_equal']['pass']:+.2f} | {old['unknown']:.2%} / {new['unknown']:.2%} |")
    lines += ["", "原32种子、96变体、正常任务及新增批量能力的全层对比、taxonomy升降、安全事件、误拒候选、故障与攻击实际触达分母均见comparison.json。", "",
              "## 简历事实草稿", "", resume, "", "## 限制", ""] + ["- " + item for item in limitations]
    (out / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"report": str(out / "comparison.md"), "case_count": 128, "trials_per_revision": 744,
            "overall_case_equal_delta_percentage_points": overall["delta_percentage_points"]["case_equal"]["pass"],
            "human_review": "pending", "release_gate": "not_assessed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--change-registry", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(compare(args.before, args.after, args.change_registry, args.out), ensure_ascii=False))


if __name__ == "__main__":
    main()
