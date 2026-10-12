#!/usr/bin/env python3
"""Read-only V2 metrics, calibration worksheets and a conservative release gate."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import importlib.util
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from billingops.evaluation.v2_metrics import VERSION, digest, load_archive, scientific_metrics, sha
from billingops.evaluation.v2_calibration import build_review_packet, evaluate_calibration, review_template
from billingops.evaluation.v2_release import evaluate_release, make_release_protocol


def write(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def report(run: Path, out: Path, *, review_files: list[Path] | None = None,
           max_per_stratum: int = 25, retry: Path | None = None,
           baseline_report: Path | None = None, change_registry: Path | None = None) -> dict:
    run, out = run.resolve(), out.resolve()
    if out == run or out.is_relative_to(run):
        raise ValueError("output must be outside the immutable run")
    composite = None
    if retry is not None:
        if baseline_report is None or change_registry is None:
            raise ValueError("authorized retry requires baseline_report and change_registry")
        if out == retry.resolve() or out.is_relative_to(retry.resolve()):
            raise ValueError("output must be outside the immutable retry")
        spec = importlib.util.spec_from_file_location("scientific_retry_validation", Path(__file__).with_name("analyze_controlled_retry_v2.py"))
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        composite = helper.validate_inputs(run, retry, baseline_report, change_registry)
        manifest, tasks, rows, artifacts = (composite[k] for k in ("manifest", "tasks", "rows", "artifacts"))
        integrity = {"ok": True, "complete_plan": True, "ordinary_complete_run": False,
            "mode": "previously_authorized_infrastructure_retry_composite",
            "base": composite["base_integrity"], "retry": composite["retry_integrity"],
            "logical_trial_slots": len(rows), "actual_executed_trials": len(composite["base_rows"]) + len(composite["retry_rows"]),
            "input_hashes": composite["hashes"], "replacement_lineage": composite["lineage"]}
    else:
        if baseline_report is not None or change_registry is not None:
            raise ValueError("baseline_report/change_registry require retry")
        manifest, tasks, rows, artifacts, integrity = load_archive(run)
    # Refuse overwriting earlier analysis as well as execution evidence.
    out.mkdir(parents=True, exist_ok=False)
    metrics = scientific_metrics(manifest, rows, artifacts, tasks=tasks)
    if composite is not None:
        from billingops.evaluation.v2_statistics import resource_distributions
        metrics["resources_logical_selected_not_total_spend"] = metrics["resources"]
        metrics["resources"] = resource_distributions(composite["base_rows"] + composite["retry_rows"])
        metrics["resource_population"] = "all actual attempts including replaced original failures; result/process statistics use the authorized logical slots"
    versions = {**manifest["evaluation_versions"], "runner": manifest["runner_version"],
                "model": manifest.get("model"), "source_bundle_sha256": digest({
                    key: manifest["source"].get(key) for key in ("source_digests", "tool_schemas_sha256")})}
    packet = build_review_packet(rows, artifacts, versions=versions,
        protocol={"version": "calibration-sampling-1", "max_per_stratum": max_per_stratum,
                  "high_risk_reviews": 2, "ordinary_reviews": 1})
    reviews = [json.loads(path.read_text()) for path in review_files or []]
    calibration = evaluate_calibration(packet, reviews, artifacts=artifacts)
    protocol = manifest.get("release_protocol")
    gate = evaluate_release(rows, manifest["planned_trials"], manifest["case_profiles"],
        protocol=protocol, calibration=calibration,
        provenance={"artifacts_verified": integrity["ok"],
                    "protocol_frozen_before_execution": bool(protocol),
                    "version_bundle": versions, "hidden_tasks": 0,
                    "hidden_unexposed": False, "calibration_independence_verified": False})
    sources = [Path(__file__).resolve()] + [ROOT / "src/billingops/evaluation" / name for name in (
        "v2_metrics.py", "v2_process_metrics.py", "v2_statistics.py", "v2_calibration.py", "v2_release.py")]
    if composite is not None:
        sources += [Path(__file__).with_name(name) for name in ("analyze_controlled_retry_v2.py", "analyze_controlled_v2.py", "compare_controlled_v2.py")]
    source_dir = out / "measurement-source"
    source_dir.mkdir()
    for path in sources:
        shutil.copyfile(path, source_dir / path.name)
    result = {"version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
              "run": str(run), "integrity": integrity, "evaluation_versions": versions,
              "scientific_metrics": metrics, "calibration": calibration, "release_gate": gate,
              "release_status": gate["status"], "measurement_source_sha256": {p.name: sha(p) for p in sources},
              "review_inputs": [{"path": str(p.resolve()), "sha256": sha(p)} for p in review_files or []],
              "scope": "supplement to the verified authorized retry composite" if composite else "supplement to this exact frozen run; not retry-merged, not current Agent performance"}
    write(out / "report.json", result)
    write(out / "calibration-packet.json", packet)
    write(out / "human-review-template.json", review_template(packet))
    write(out / "calibration-report.json", calibration)
    write(out / "release-gate.json", gate)
    write(out / "release-protocol-proposal.json", make_release_protocol(versions=versions))
    process = metrics["process"]["all_trials_diagnostic"]
    stats = metrics["statistics"]
    compact_aggregates = {weight: {k: {key: value for key, value in values.items() if not key.endswith("_ids")} for k, values in by_k.items()}
                          for weight, by_k in (stats["aggregates"] or {}).items()}
    text = ["# V2 评测指标补充报告", "",
        f"输入：`{run}`。{len(manifest['planned_trials'])} 个 case，{len(rows)}/{sum(manifest['planned_trials'].values())} 个预登记 trial。",
        "原始评分、轨迹和版本全部保留。本报告新增观测口径，不代表当前 Agent 成绩或 Agent 改进收益。", "",
        f"实际资源统计包含 {metrics['resources']['all_trials']['trial_count']} 次执行；重试合并遵守原已授权登记，原中断目录保持中断身份。" if composite else "本报告不选择或自动补跑失败 trial。", "",
        f"发布门禁：**{gate['status']}**，release_permitted={gate['release_permitted']}。", "",
        "## 结果与统计区间", "",
        "Pass@k / Pass^k 按固定 k 和 case 等权汇总；交换性/IID 未获证明时只解释为重复试验的描述性组合统计。",
        "unknown 敏感性上下界与根族 bootstrap 区间分开。开发集区间不能证明生产泛化；缺失试验时不输出正式总体。", "",
        "```json", json.dumps({"status": stats["status"], "aggregates": compact_aggregates or None, "bootstrap": stats["bootstrap"]}, ensure_ascii=False, indent=2), "```", "",
        "statistics_by_operator 单独列出原始题（NONE）及每种扰动；总体分数代表预登记的混合开发分布。", "",
        "## 过程与澄清", "",
        "工具响应成功与语义正确分别计量；未有独立逐调用标签时语义正确率为 unknown。重复读取、提案作废、重新调查只作为候选诊断。", "",
        "```json", json.dumps({k: process[k] for k in ("tool_transport", "tool_semantic_correctness", "necessary_evidence", "clarification")}, ensure_ascii=False, indent=2), "```", "",
        "## 资源与校准", "",
        "完整 JSON 同时提供所有执行、成功且安全检查明确通过的试验：各角色调用数、tokens、模型估算费用、工具费用和 wall-time 的均值/P50/P95及缺失数。历史工具费用缺少明确报价时保持 unknown。", "",
        f"裁判抽样：{packet['sampling']['selected_claims']} 条 / {packet['sampling']['population_claims']} 条原子判定。人工校准状态：`{calibration['status']}`。", "",
        "将 human-review-template.json 分别交给独立复核者填写；高风险判定需要两名复核者。模板不含裁判标签。身份声明只是一项记录，发布还需要可信流程确认身份与独立性。", "",
        "release-protocol-proposal.json 仅是待审的阈值提案，不是本历史运行的预登记，也不代表发布批准。现有输入没有未曝光 Hidden 验收证明。", "",
        "## 文件", "", "- [完整指标](report.json)", "- [独立人工复核模板](human-review-template.json)",
        "- [校准绑定与分层抽样](calibration-packet.json)", "- [校准报告](calibration-report.json)",
        "- [发布阻断理由](release-gate.json)", "- [下轮协议提案](release-protocol-proposal.json)", ""]
    (out / "report.md").write_text("\n".join(text), encoding="utf-8")
    return {"report": str(out / "report.md"), "trials": len(rows), "release_status": gate["status"],
            "calibration_status": calibration["status"], "review_claims": packet["sampling"]["selected_claims"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--review", type=Path, action="append", default=[], help="Completed worksheet, repeated per independent reviewer")
    parser.add_argument("--max-per-stratum", type=int, default=25)
    parser.add_argument("--retry", type=Path, help="Previously authorized retry archive; never starts a retry")
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument("--change-registry", type=Path)
    parser.add_argument("--require-release-pass", action="store_true", help="Exit 3 unless the evaluated release gate permits release")
    args = parser.parse_args()
    result = report(args.run, args.out, review_files=args.review, max_per_stratum=args.max_per_stratum,
                    retry=args.retry, baseline_report=args.baseline_report, change_registry=args.change_registry)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.require_release_pass and result["release_status"] != "pass":
        raise SystemExit(3)


if __name__ == "__main__":
    main()
