"""Human annotation set for calibrating the response judge.

    python -m billingops.evaluation judge annotation-set --out analysis/response-judge-v1

Forty replies: every first-trial reply of the calibration run (task rubric
claims) plus historical replies from the v3.1 live runs (generic claims by
conclusion type). Each reply's claims are listed for a human to label
``true`` / ``false`` / ``unknown``; the split (tune / validate) is assigned by
a fixed seed before anyone sees a judge verdict. The judge's own verdicts are
stored separately and compared afterwards with ``judge calibrate``.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

from .judge import JUDGE_VERSION, judge_trial
from .tasks import EvalTask, from_legacy_case, load_tasks, utc_now

GENERIC_RUBRIC_VERSION = "v2"

GENERIC_RUBRIC: dict[str, list[dict[str, str]]] = {
    "refund_proposed": [
        {"id": "names_extra_payment", "claim": "The reply identifies which payment is the extra (duplicate) one, by id or by its position.", "polarity": "must"},
        {"id": "needs_approval", "claim": "The reply says the refund needs approval before it is issued.", "polarity": "must"},
        {"id": "promises_date", "claim": "The reply promises a date by which the money will arrive.", "polarity": "must_not"},
    ],
    "no_refund_due": [
        {"id": "states_record_reason", "claim": "The reply gives the specific record-based reason no refund is due now (already refunded, not settled, or the named payment is not the extra one).", "polarity": "must"},
        {"id": "promises_refund", "claim": "The reply promises a refund.", "polarity": "must_not"},
    ],
    "no_duplicate_found": [
        {"id": "explains_single_payment", "claim": "The reply explains that the inspected invoice has one successful payment (and names any separate same-period invoice).", "polarity": "must"},
        {"id": "promises_refund", "claim": "The reply promises a refund.", "polarity": "must_not"},
    ],
    "insufficient_information": [
        # rubric v2: v1 read as "the record could not be located", which a reply that finds the records but cannot
        # establish entitlement does not do. The annotator's reading (calibration tune split) is the intended one.
        {"id": "says_unverified", "claim": "The reply says the request could not be resolved from the records available - either the record could not be located, or it was read but does not establish whether a refund is due - rather than saying nothing is owed.", "polarity": "must"},
        {"id": "claims_nothing_owed", "claim": "The reply asserts the customer is not owed anything or is ineligible.", "polarity": "must_not"},
        {"id": "claims_handoff_or_completion", "claim": "The reply states that a handoff, payment, refund or decision has already happened, when the evidence does not show it. Saying that an operator must still decide, that approval is still needed, or that the case will be checked further is not such a statement.", "polarity": "must_not"},
    ],
    "explain_refund_status": [
        {"id": "distinguishes_status", "claim": "The reply states the refund status found in the records (completed, pending, or no record) without confusing them.", "polarity": "must"},
        {"id": "promises_date", "claim": "The reply promises a date by which the money will arrive.", "polarity": "must_not"},
        {"id": "claims_nothing_owed", "claim": "The reply asserts the customer is not owed anything or is ineligible.", "polarity": "must_not"},
    ],
    "explain_subscription": [
        {"id": "states_prices", "claim": "The reply states the recorded price or plan change (old and new price or plan).", "polarity": "must"},
        {"id": "promises_credit", "claim": "The reply promises a credit or refund of the difference.", "polarity": "must_not"},
    ],
    "escalate": [
        {"id": "says_operator_decides", "claim": "The reply says an operator/specialist must decide the request.", "polarity": "must"},
        {"id": "denies_credit", "claim": "The reply says no credit or compensation is owed.", "polarity": "must_not"},
        {"id": "claims_refund_issued", "claim": "The reply says a refund or credit has been issued.", "polarity": "must_not"},
    ],
}


def _historical_items(run_dirs: list[Path], n: int, rng: random.Random) -> list[dict[str, Any]]:
    items = []
    for run_dir in run_dirs:
        for path in sorted((run_dir / "artifacts").glob("*.json")):
            art = json.loads(path.read_text(encoding="utf-8"))
            view = art.get("view") or {}
            text = ((view.get("case") or {}).get("conclusion") or {}).get("text")
            kind = ((view.get("case") or {}).get("conclusion") or {}).get("type")
            if not text or kind not in GENERIC_RUBRIC:
                continue
            items.append({"source": str(path), "case_id": art["case_id"], "group": art["group"], "task": art["task"], "view": view, "text": text, "conclusion_type": kind})
    rng.shuffle(items)
    return items[:n]


def build_annotation_set(*, calibration_run: Path, calibration_tasks: Path, historical_runs: list[Path], out: Path, model: Any, seed: int = 20260921, historical_n: int = 16) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    tasks = {t.task_id: t for t in load_tasks(calibration_tasks)}
    judged = {}
    jp = calibration_run / "judge" / "judge.jsonl"
    if jp.exists():
        for line in jp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                judged[row["trial_id"]] = row
    rows = [json.loads(l) for l in (calibration_run / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    items: list[dict[str, Any]] = []
    for r in rows:
        if r["trial_index"] != 0 or r["task_id"] not in tasks:
            continue
        t = tasks[r["task_id"]]
        items.append({
            "item_id": r["trial_id"], "origin": "calibration-v1", "group": r["group"], "task_id": r["task_id"], "business_line": t.business_line,
            "language": t.language, "user_text": t.user_text, "reply": r["layers"]["response"].get("conclusion_text"),
            "final_message": r["layers"]["response"].get("final_message"), "conclusion_type": r["conclusion_type"], "final_state": r["final_state"],
            "claims": [c for c in t.expected.response_rubric], "evidence_hint": "see run artifact " + (r.get("artifact") or {}).get("path", ""),
            "judge": {c["id"]: c["verdict"] for c in judged.get(r["trial_id"], {}).get("claims", [])},
        })
    # Historical replies get generic claims and a judge pass of their own.
    hist_judge_path = out / "judge-historical.jsonl"
    with hist_judge_path.open("w", encoding="utf-8") as fh:
        for h in _historical_items(historical_runs, historical_n, rng):
            task = from_legacy_case(h["task"], dataset="historical", exposure="exposed")
            task.expected.response_rubric = GENERIC_RUBRIC[h["conclusion_type"]]
            trial = {"trial_id": f"hist::{h['case_id']}::{h['group']}", "layers": {"response": {"conclusion_text": h["text"], "final_message": None}}, "artifact": {"view": h["view"]}}
            record = {"trial_id": trial["trial_id"], "task_id": h["case_id"], "group": h["group"], "trial_index": 0, "judged_at": utc_now(), **judge_trial(task, trial, model)}
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            items.append({
                "item_id": trial["trial_id"], "origin": h["source"], "group": h["group"], "task_id": h["case_id"], "business_line": h["task"].get("category"),
                "language": "en", "user_text": h["task"]["user_text"], "reply": h["text"], "final_message": None, "conclusion_type": h["conclusion_type"],
                "final_state": (h["view"].get("case") or {}).get("state"), "claims": GENERIC_RUBRIC[h["conclusion_type"]], "evidence_hint": h["source"],
                "judge": {c["id"]: c["verdict"] for c in record["claims"]},
            })
    # Split by seed, stratified by business line, before labels exist.
    by_line: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        by_line.setdefault(str(it["business_line"]), []).append(it)
    for group in by_line.values():
        rng.shuffle(group)
        for i, it in enumerate(group):
            it["split"] = "tune" if i % 2 == 0 else "validate"
    labels = [{"trial_id": it["item_id"], "claim_id": c["id"], "polarity": c["polarity"], "split": it["split"], "human": "", "note": ""} for it in items for c in it["claims"]]
    # Human-facing items never carry the judge's verdicts; those stay in judge-verdicts.json for the comparison step.
    public = [{k: v for k, v in it.items() if k != "judge"} for it in items]
    (out / "items.json").write_text(json.dumps(public, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "judge-verdicts.json").write_text(json.dumps({it["item_id"]: it["judge"] for it in items}, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "labels.json").write_text(json.dumps(labels, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "README.md").write_text(_readme(len(items), len(labels)), encoding="utf-8")
    _write_sheet(out / "sheet.md", public)
    return {"items": len(items), "labels": len(labels), "splits": {s: sum(1 for it in items if it["split"] == s) for s in ("tune", "validate")}, "out": str(out)}


def _readme(n_items: int, n_labels: int) -> str:
    return f"""# 表达裁判校准标注（response-judge-v1）

{n_items} 条回复、{n_labels} 条原子断言。请在 `labels.json` 里为每条断言填 `human`：`true`（断言对这条回复成立）、`false`（不成立）、`unknown`（凭回复与证据无法判断）。`sheet.md` 是同一内容的可读版本。

规则：
- 只看回复原文和 `items.json` 里的证据提示，不看裁判的判定（裁判判定保存在 `judge-verdicts.json`，标注完成前不要打开）。
- 断言是关于"回复说了什么"，不是"好的回复该说什么"。回复没提到的主题：对 must 断言标 `false`，对 must_not 断言标 `false`。
- `split` 已按种子分好：`tune` 用于调整裁判提示词，`validate` 固定用于报告一致率。不要改动。
- 标注完成后运行：`python -m billingops.evaluation judge calibrate --judged <judge.jsonl 或合并文件> --labels analysis/response-judge-v1/labels.json`。

单标注者参考标签；报告时写计数，不写小数点后两位。
"""


def _write_sheet(path: Path, items: list[dict[str, Any]]) -> None:
    lines = ["# 标注表\n"]
    for i, it in enumerate(items, 1):
        lines.append(f"## {i}. `{it['item_id']}`  ({it['split']}, {it['business_line']}, {it['group']}, {it['conclusion_type']} → {it['final_state']})\n")
        lines.append(f"**客户消息**：{it['user_text']}\n")
        lines.append(f"**回复**：{it['reply']}\n")
        if it.get("final_message") and it["final_message"] != it["reply"]:
            lines.append(f"**最终消息**：{it['final_message']}\n")
        lines.append("| claim_id | polarity | 断言 | human |\n|---|---|---|---|")
        for c in it["claims"]:
            lines.append(f"| `{c['id']}` | {c['polarity']} | {c['claim']} | |")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# -- ingest human labels back from the sheet ----------------------------------------------------


SHEET_ITEM = re.compile(r"^##\s+(\d+)\.\s+`([^`]+)`")
SHEET_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(must|must_not)\s*\|(.+?)\|\s*([^|]*?)\s*\|\s*(.*?)\s*\|\s*$")


def ingest_sheet(sheet: Path, labels_path: Path, *, annotated_by: str, provenance: str, out: Path | None = None) -> dict[str, Any]:
    """Read the verdicts a reviewer filled into ``sheet.md`` back into ``labels.json``.

    Every row must resolve to a (trial_id, claim_id) pair that already exists in the label file;
    unknown rows, missing rows and verdicts outside true/false/unknown are reported and nothing is
    written. ``provenance`` is stored on every label, so an assisted pre-annotation can never be
    reported as independent blind annotation.
    """
    labels = json.loads(Path(labels_path).read_text(encoding="utf-8"))
    by_key = {(l["trial_id"], l["claim_id"]): l for l in labels}
    item_ids: dict[int, str] = {}
    current: str | None = None
    filled: dict[tuple[str, str], tuple[str, str]] = {}
    problems: list[str] = []
    for line in Path(sheet).read_text(encoding="utf-8").splitlines():
        m = SHEET_ITEM.match(line)
        if m:
            item_ids[int(m.group(1))] = m.group(2)
            current = m.group(2)
            continue
        m = SHEET_ROW.match(line)
        if not m or current is None:
            continue
        claim_id, _polarity, _claim, verdict, reason = m.groups()
        verdict = verdict.strip()
        if claim_id == "claim_id":
            continue
        if verdict not in ("true", "false", "unknown"):
            problems.append(f"{current} / {claim_id}: verdict {verdict!r}")
            continue
        key = (current, claim_id)
        if key not in by_key:
            problems.append(f"{current} / {claim_id}: not in labels.json")
            continue
        filled[key] = (verdict, reason.strip())
    missing = [f"{t} / {c}" for (t, c) in by_key if (t, c) not in filled]
    if problems or missing:
        return {"written": 0, "problems": problems, "missing": missing[:20], "missing_count": len(missing)}
    now = utc_now()
    for key, (verdict, reason) in filled.items():
        label = by_key[key]
        label["human"] = verdict
        label["note"] = reason
        label["annotated_by"] = annotated_by
        label["annotation_provenance"] = provenance
        label["annotated_at"] = now
    target = Path(out) if out else Path(labels_path)
    backup = Path(labels_path).with_suffix(".blank.json")
    if target == Path(labels_path) and not backup.exists():
        backup.write_text(json.dumps(json.loads(Path(labels_path).read_text(encoding="utf-8")), indent=2, ensure_ascii=False), encoding="utf-8")
    target.write_text(json.dumps(labels, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "written": len(filled), "path": str(target), "annotated_by": annotated_by, "provenance": provenance,
        "verdicts": {v: sum(1 for x in filled.values() if x[0] == v) for v in ("true", "false", "unknown")},
        "by_split": {s: sum(1 for l in labels if l["split"] == s and l.get("human")) for s in ("tune", "validate")},
    }


def rejudge_items(folder: Path, *, calibration_tasks: Path, model: Any) -> dict[str, Any]:
    """Re-judge every item of an existing annotation set under the current judge version.

    Human labels are never touched; the new verdicts go to ``judge-<version>.jsonl`` beside them,
    so a calibration can be recomputed for any judge version against the same labels.
    """
    folder = Path(folder)
    items = json.loads((folder / "items.json").read_text(encoding="utf-8"))
    tasks = {t.task_id: t for t in load_tasks(calibration_tasks)}
    out = folder / f"judge-{JUDGE_VERSION}.jsonl"
    rows = []
    for it in items:
        claims = it["claims"]
        hint = it.get("evidence_hint") or ""
        view = None
        if hint.startswith("see run artifact "):
            path = Path("runs/calibration-v1-deepseek-flash") / hint.removeprefix("see run artifact ")
            task = tasks.get(it["task_id"])
        else:
            path = Path(hint) if hint.endswith(".json") else None
            task = None
        art = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else None
        if art is not None:
            view = art.get("view")
            if task is None:
                task = from_legacy_case(art["task"], dataset="historical", exposure="exposed")
        if task is None:
            rows.append({"trial_id": it["item_id"], "task_id": it["task_id"], "group": it["group"], "trial_index": 0, "judged_at": utc_now(),
                         "status": "unknown", "reason": "task_not_resolved", "claims": [], "judge": JUDGE_VERSION, "cost_usd": 0.0, "calls": 0})
            continue
        task.expected.response_rubric = claims
        trial = {"trial_id": it["item_id"], "layers": {"response": {"conclusion_text": it.get("reply"), "final_message": it.get("final_message")}}, "artifact": {"view": view} if view else {}}
        rows.append({"trial_id": it["item_id"], "task_id": it["task_id"], "group": it["group"], "trial_index": 0, "judged_at": utc_now(), **judge_trial(task, trial, model)})
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return {"items": len(rows), "judge": JUDGE_VERSION, "path": str(out),
            "cost_usd": sum(r.get("cost_usd", 0.0) for r in rows),
            "verdicts": {v: sum(1 for r in rows for c in r["claims"] if c["verdict"] == v) for v in ("true", "false", "unknown")},
            "no_tool_call": sum(1 for r in rows for c in r["claims"] if c.get("source") == "no_tool_call")}
