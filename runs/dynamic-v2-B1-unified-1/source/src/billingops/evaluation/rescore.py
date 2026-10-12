"""Versioned partial/full rescoring, never substituting receipts for a world dump."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .artifacts import load_results, load_trial, new_output, source_manifest, task_index, utc_now, write_json
from .scorer import score_case
from .stats import bootstrap_by_family, mcnemar_exact, paired_difference, paired_resource_differences

SCORER_VERSION = "audit-v1"


def grade(ground_truth: dict, view: dict | None, refunds: list | None, legacy: dict | None = None) -> dict[str, Any]:
    if view is None:
        return {"status": "unscorable", "reason": "missing_application_artifact", "checks": {}}
    try:
        result = score_case(ground_truth=ground_truth, view=view, refunds_on_target=refunds or [])
    except Exception as exc:
        return {"status": "unscorable", "reason": "grader_error", "error_type": type(exc).__name__, "checks": {}}
    checks = {k: {"value": v, "provenance": "recomputed"} for k, v in result["checks"].items()}
    if refunds is None:
        old = (legacy or {}).get("checks", {})
        checks["db_state"] = {"value": old.get("db_state"), "provenance": "legacy" if "db_state" in old else "unavailable"}
    # A known failed conjunct suffices to establish failure even without the final world.
    failed = any(v["value"] is False and v["provenance"] == "recomputed" for v in checks.values())
    complete = all(v["provenance"] == "recomputed" for v in checks.values())
    return {"status": "fail" if failed else "pass" if complete else "unscorable", "reason": None if complete else "billing_final_not_archived", "checks": checks}


def refunds_from_world(world: dict | None, task: dict, view: dict) -> list | None:
    if world is None:
        return None
    if not isinstance(world.get("refunds"), list) or not isinstance(world.get("refund_requests"), list):
        raise ValueError("incomplete Billing world dump")
    truth = task["ground_truth"]
    target = truth.get("db_check_payment_id") or truth.get("expected_target_payment_id") or (view["case"].get("proposal") or {}).get("payment_id")
    customer = view["case"]["customer_id"]
    return [r for r in world["refunds"] if r.get("customer_id") == customer and (not target or r.get("payment_id") == target)]


def process_checks(trial: dict) -> dict[str, Any]:
    events = trial.get("events", [])
    by_kind: dict[str, list[int]] = {}
    for e in events:
        by_kind.setdefault(e["kind"], []).append(e["seq"])
    sends, approvals, verified = (by_kind.get(k, []) for k in ("refund_sending", "approval_recorded", "refund_verified"))
    closures = [e["seq"] for e in events if e["kind"] == "state_changed" and e["payload"].get("to") == "closed"]
    original = trial.get("original_result") or {}
    failed = {k for k,v in original.get("checks", {}).items() if v is False}
    stage = "runtime" if original.get("runtime_failure") else "proposal" if failed & {"target", "amount"} else "investigation" if "facts" in failed else "conclusion_or_disposition" if failed & {"conclusion", "verdict_reason", "final_state"} else "execution_verification" if failed & {"db_state", "executed_and_verified"} else "scope" if "scope" in failed else None
    return {
        "failure_stage_hint": stage,
        "localization_basis": "original failed checks and stored events; not a causal root-cause verdict",
        "approval_before_send": None if not sends else bool(approvals) and min(approvals) < min(sends),
        "single_send_recorded": len(sends) <= 1,
        "verification_before_refund_close": None if not sends else bool(verified) and bool(closures) and min(verified) < max(closures),
        "event_count": len(events),
        "evidence_reads": [{"tool": e["tool"], "ok": e.get("ok"), "phase": e.get("phase")} for e in (trial.get("view") or {}).get("evidence", [])],
        "note": "diagnostics only; no exact reference tool trajectory is required",
    }


def summarise_grades(rows: list[dict], originals: list[dict]) -> dict:
    index = {(r["case_id"], r["group"]): r for r in rows}
    ids = sorted({r["case_id"] for r in originals})
    pairs = []
    excluded = []
    for cid in ids:
        a, b = index.get((cid, "A")), index.get((cid, "B"))
        if not a or not b or any(r["status"] == "unscorable" for r in (a, b)):
            excluded.append({"case_id": cid, "reasons": {g: index.get((cid, g), {}).get("reason", "missing_group") for g in ("A", "B") if index.get((cid, g), {}).get("status") not in ("pass", "fail")}})
            continue
        pairs.append({"family": a["family"], "a": a["status"] == "pass", "b": b["status"] == "pass"})
    return {
        "total_case_ids": len(ids), "evaluable_pairs": len(pairs), "excluded_pairs": excluded,
        "coverage": len(pairs) / len(ids) if ids else None,
        "per_group": {g: dict(Counter(r["status"] for r in rows if r["group"] == g)) for g in ("A", "B")},
        "paired_success": paired_difference(pairs), "bootstrap": bootstrap_by_family(pairs),
        "mcnemar": mcnemar_exact(pairs),
        "resources_all_runs": paired_resource_differences(originals),
        "cost_population": "all original runs, including failures and unscorable trials",
        "check_coverage": {name: dict(Counter(r.get("checks", {}).get(name, {}).get("provenance", "unavailable") for r in rows)) for name in sorted({k for r in rows for k in r.get("checks", {})})},
    }


def rescore_run(run_dir: Path, *, tasks_path: Path, output: Path | None = None) -> Path:
    originals, tasks = load_results(run_dir), task_index(tasks_path)
    out = new_output(run_dir, output, "rescore")
    rows = []
    for old in originals:
        meta = {"case_id": old["case_id"], "group": old["group"], "family": old["family"], "original_success": old["success"], "scorer_version": SCORER_VERSION}
        try:
            trial = load_trial(run_dir, old, tasks)
            if not trial.get("task"):
                raise ValueError("missing task specification")
            refunds = refunds_from_world(trial.get("billing_final"), trial["task"], trial["view"])
            result = grade(trial["task"]["ground_truth"], trial["view"], refunds, old)
            meta.update(task_provenance=trial["task_provenance"], process=process_checks(trial))
        except Exception as exc:
            result = {"status": "unscorable", "reason": "missing_or_invalid_artifact", "detail": str(exc), "checks": {}}
        # Recorded runtime failure is not reclassified as missing evidence or a grader failure.
        if old.get("runtime_failure"):
            result.update(status="fail", reason="recorded_runtime_failure")
        rows.append({**meta, **result})
    summary = summarise_grades(rows, originals)
    write_json(out / "grades.json", rows)
    write_json(out / "summary.json", summary)
    write_json(out / "audit_manifest.json", {"source_run": str(run_dir.resolve()), "created_at": utc_now(), "scorer_version": SCORER_VERSION, **source_manifest(historical=True)})
    complete = bool(rows) and all(r.get("checks") and all(c["provenance"] == "recomputed" for c in r["checks"].values()) and r.get("task_provenance") == "captured_at_execution" for r in rows)
    lines = ["# " + ("基于完整存档的重评分报告" if complete else "案例复核及部分重评分报告"), "", "原始实验结果未修改。本报告是回顾性审计，不是新的模型试验，也不是预注册分析。", "", f"案例总数：{summary['total_case_ids']}；有效配对：{summary['evaluable_pairs']}；覆盖率：{summary['coverage']:.1%}。", "", "| 组别 | pass | fail | unscorable |", "|---|---:|---:|---:|"]
    for g, counts in summary["per_group"].items():
        lines.append(f"| {g} | {counts.get('pass', 0)} | {counts.get('fail', 0)} | {counts.get('unscorable', 0)} |")
    lines += ["", "缺少 Billing 终态时，db_state 保留 legacy 标记。退款回执只能证明局部动作；没有据此重建数据库终态。已重算的必要条件失败可确认 fail，其余缺少必要证据的运行不确认 pass。历史检查使用目前可用任务规范，task_provenance 标明其是否随运行归档。完整存档只表示现有评分规则所需输入齐全，不表示所有质量维度均已评估。", "", "一侧不可评分则整对排除成功率比较；费用和耗时保留全部原始运行。没有有效配对时，不发布新的成功率差异或显著性结论。", "", "## 检查输入覆盖", "", "| 检查 | recomputed | legacy | unavailable |", "|---|---:|---:|---:|"]
    for name, counts in summary["check_coverage"].items():
        lines.append(f"| {name} | {counts.get('recomputed',0)} | {counts.get('legacy',0)} | {counts.get('unavailable',0)} |")
    lines += ["", "## 配对资源差异（B − A）", "", "| 指标 | 配对数 | 平均差异 | 探索性模板聚类区间 |", "|---|---:|---:|---|"]
    for metric, data in summary["resources_all_runs"].items():
        lines.append(f"| {metric} | {data['n']} | {data['mean_difference']} | {data['interval']} |")
    lines += ["", "v1 与 v2 含适配器变化，不能作为同版本重复试验估计方差。稳定性尚未量化。新评分口径、审查标准和成本分析均为回顾性增补。", "", "详情见 grades.json、summary.json 与 audit_manifest.json。"]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    return out
