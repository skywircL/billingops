"""Turn results.jsonl into the preregistered report."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .stats import bootstrap_by_family, mcnemar_exact, mean, paired_difference, stratified, paired_resource_differences


def load_results(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def pair_up(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_case: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in results:
        by_case[r["case_id"]][r["group"]] = r
    pairs = []
    for case_id, groups in sorted(by_case.items()):
        if "A" in groups and "B" in groups:
            a, b = groups["A"], groups["B"]
            if any(r.get("score_status") == "unscorable" or r.get("success") is None for r in (a, b)):
                continue
            pairs.append({"case_id": case_id, "family": a["family"], "category": a["category"], "a": a["success"], "b": b["success"], "A": a, "B": b})
    return pairs


def _group_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    scorable = [r for r in rows if r.get("success") is not None and r.get("score_status") != "unscorable"]
    return {
        "n": n,
        "success_rate": (sum(1 for r in scorable if r["success"]) / len(scorable)) if scorable else None,
        "scorable_n": len(scorable),
        "unscorable_n": n - len(scorable),
        "cost_usd_mean": mean([r["cost_usd"] for r in rows]),
        "cost_usd_total": sum(r["cost_usd"] for r in rows),
        "model_calls_mean": mean([r["model_calls"] for r in rows]),
        "reads_mean": mean([r["reads"] for r in rows]),
        "clarifications_mean": mean([r["clarifications"] for r in rows]),
        "latency_s_mean": mean([r["latency_s"] for r in rows]),
        "budget_exhausted_rate": (sum(1 for r in rows if r["budget_exhausted"]) / n) if n else None,
        "runtime_failures": Counter(r["runtime_failure"] for r in rows if r["runtime_failure"]),
        "blocked_proposals": sum(r["blocked_proposals"] for r in rows),
        "policy_violations": sum(1 for r in rows if r["policy_violation"]),
        "gate_rejections": sum(r["gate_rejections"] for r in rows),
    }


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    pairs = pair_up(results)
    groups = {g: [r for r in results if r["group"] == g] for g in ("A", "B")}
    infra_excluded = [p for p in pairs if not (p["A"]["runtime_failure"] == "infra" or p["B"]["runtime_failure"] == "infra")]
    summary: dict[str, Any] = {
        "runs": len(results),
        "pairs": len(pairs),
        "total_case_ids": len({r["case_id"] for r in results}),
        "excluded_unscorable_pairs": len({r["case_id"] for r in results}) - len(pairs),
        "unscorable_runs": [r for r in results if r.get("score_status") == "unscorable" or r.get("success") is None],
        "paired_resources": paired_resource_differences(results),
        "families": len({p["family"] for p in pairs}),
        "primary": paired_difference(pairs),
        "bootstrap_by_family": bootstrap_by_family(pairs),
        "strata": stratified(pairs, "category"),
        "per_group": {g: _group_metrics(rows) for g, rows in groups.items()},
        "per_group_by_category": {g: {cat: _group_metrics([r for r in rows if r["category"] == cat]) for cat in sorted({r["category"] for r in rows})} for g, rows in groups.items()},
        "appendix": {
            "mcnemar": mcnemar_exact(pairs),
            "sensitivity_excluding_infra_failures": {
                "excluded_pairs": len(pairs) - len(infra_excluded),
                "rule": "exclude a pair when either side failed on Billing or model-API infrastructure (5xx, connection, timeout, rate limit). Budget exhaustion, protocol repairs and software errors are never excluded.",
                **paired_difference(infra_excluded),
            },
        },
        "failures": [
            {"case_id": r["case_id"], "group": r["group"], "family": r["family"], "final_state": r["final_state"], "conclusion": r["conclusion_type"], "failed_checks": [k for k, v in r["checks"].items() if not v], "runtime_failure": r["runtime_failure"], "policy_violation": r["policy_violation"]}
            for r in results
            if r["success"] is False
        ],
    }
    return summary


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _num(x: float | None, fmt: str = "{:.2f}") -> str:
    return "-" if x is None else fmt.format(x)


def render_markdown(summary: dict[str, Any], manifest: dict[str, Any]) -> str:
    p = summary["primary"]
    bs = summary["bootstrap_by_family"]
    lines = [
        f"# BillingOps experiment report: {manifest.get('label', '')}",
        "",
        f"Model: `{manifest.get('model')}`. Runs: {summary['runs']} ({summary['pairs']} paired cases, {summary['families']} template families).",
        f"{manifest.get('note', '')}",
        f"Evaluable pairs: {summary['pairs']}/{summary['total_case_ids']}; excluded incomplete/unscorable pairs: {summary['excluded_unscorable_pairs']}. Costs and latency retain all runs.",
        "",
        "## Primary metric: overall case success (paired A/B)",
        "",
        "| | A fixed workflow | B dynamic investigation | B − A |",
        "|---|---|---|---|",
        f"| success rate (n={p['n']}) | {_pct(p['rate_a'])} | {_pct(p['rate_b'])} | {_num(p['diff'], '{:+.3f}')} |",
        "",
        f"Family bootstrap: {bs['note']}." + (f" Interval for B − A: [{bs['interval'][0]:+.3f}, {bs['interval'][1]:+.3f}]." if bs.get("interval") and not bs["degenerate"] else ""),
        "",
        "## Stratified results (each stratum with its own n; no borrowing of the overall sample size)",
        "",
        "| stratum | n | families | A | B | B − A |",
        "|---|---|---|---|---|---|",
    ]
    for name, s in summary["strata"].items():
        lines.append(f"| {name} | {s['n']} | {s['families']} | {_pct(s['rate_a'])} | {_pct(s['rate_b'])} | {_num(s['diff'], '{:+.3f}')} |")
    lines += ["", "## Secondary metrics per group", "", "| metric | A | B |", "|---|---|---|"]
    ga, gb = summary["per_group"]["A"], summary["per_group"]["B"]
    for key, label, fmt in (
        ("cost_usd_mean", "mean cost per case (USD, all model calls)", "{:.4f}"),
        ("cost_usd_total", "total cost (USD)", "{:.2f}"),
        ("model_calls_mean", "mean model calls", "{:.2f}"),
        ("reads_mean", "mean reads", "{:.2f}"),
        ("clarifications_mean", "mean clarifications", "{:.2f}"),
        ("latency_s_mean", "mean latency (s, simulated humans answer instantly)", "{:.2f}"),
    ):
        lines.append(f"| {label} | {_num(ga[key], fmt)} | {_num(gb[key], fmt)} |")
    lines.append(f"| budget exhaustion rate | {_pct(ga['budget_exhausted_rate'])} | {_pct(gb['budget_exhausted_rate'])} |")
    lines.append(f"| runtime failures (infra / own) | {ga['runtime_failures'].get('infra', 0)} / {ga['runtime_failures'].get('own', 0)} | {gb['runtime_failures'].get('infra', 0)} / {gb['runtime_failures'].get('own', 0)} |")
    lines.append(f"| dangerous proposals blocked by code | {ga['blocked_proposals']} | {gb['blocked_proposals']} |")
    lines.append(f"| recorded policy violations (limited code checks, not an exhaustive safety audit) | {ga['policy_violations']} | {gb['policy_violations']} |")
    lines.append(f"| approvals refused at the gate | {ga['gate_rejections']} | {gb['gate_rejections']} |")
    dup = summary["per_group_by_category"]
    if "duplicate" in dup["A"]:
        lines += ["", "## Duplicate-charge subset", "", "| metric | A | B |", "|---|---|---|"]
        for key, label, fmt in (("n", "n", "{:d}"), ("success_rate", "success rate", None), ("cost_usd_mean", "mean cost (USD)", "{:.4f}"), ("reads_mean", "mean reads", "{:.2f}"), ("model_calls_mean", "mean model calls", "{:.2f}")):
            a, b = dup["A"]["duplicate"][key], dup["B"]["duplicate"][key]
            lines.append(f"| {label} | {_pct(a) if fmt is None else _num(a, fmt)} | {_pct(b) if fmt is None else _num(b, fmt)} |")
    lines += ["", "## Failure analysis", ""]
    if not summary["failures"]:
        lines.append("No failed runs.")
    for f in summary["failures"]:
        lines.append(f"- `{f['case_id']}` [{f['group']}] {f['family']}: state={f['final_state']}, conclusion={f['conclusion']}, failed checks={f['failed_checks']}" + (f", runtime failure={f['runtime_failure']}" if f["runtime_failure"] else "") + (f", violation={f['policy_violation']}" if f["policy_violation"] else ""))
    mc = summary["appendix"]["mcnemar"]
    sens = summary["appendix"]["sensitivity_excluding_infra_failures"]
    lines += [
        "",
        "## Appendix",
        "",
        f"McNemar exact test on discordant pairs: A-only successes {mc['a_only']}, B-only successes {mc['b_only']}, p = {mc['p_value']:.3f}. Assumption: {mc['assumption']}.",
        "",
        f"Sensitivity analysis excluding infrastructure failures ({sens['excluded_pairs']} pairs excluded; rule: {sens['rule']}): A {_pct(sens['rate_a'])}, B {_pct(sens['rate_b'])}, B − A {_num(sens['diff'], '{:+.3f}')} over n={sens['n']}.",
        "",
        "No non-inferiority test was run. A non-significant difference is not evidence of equal performance.",
    ]
    return "\n".join(lines) + "\n"


def write_report(out_dir: Path) -> Path:
    results = load_results(out_dir / "results.jsonl")
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    summary = summarise(results)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    report = out_dir / "report.md"
    report.write_text(render_markdown(summary, manifest), encoding="utf-8")
    return report
