"""Observability page: evaluation runs, per-family results and the case desk, in one HTML file.

    python -m billingops.evaluation dashboard                 # writes analysis/dashboard/
    python -m billingops.evaluation dashboard --out <dir>

Reads the v4 suite runs under ``runs/`` (``manifest.json`` + ``results.jsonl``, preferring a
``rescore-g2`` pass where one exists), the gate reports in ``analysis/*/release.json`` and the case
desk (``data/cases.sqlite3``, opened read-only). The data is embedded in the page, so it opens from
disk with no server and no network. Re-run after every evaluation (after ``cases import`` and
``cases mine``, so new badcases are on the desk).

Writes ``index.html`` (a complete document, for opening locally) and ``artifact.html`` (the same page
without the document skeleton, for publishing as an Artifact).

Sealed tasks appear only as counts per family: no customer message, reply, evidence, rubric text or
artifact path leaves this module for a task whose dataset is sealed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from .artifacts import utc_now

TEMPLATE = Path(__file__).with_name("dashboard_page.html")
GROUPS = ("A", "B")
CJK = re.compile(r"[一-鿿]")

# Pre-v4 phases have no suite runs in the current format; their results are quoted from the reports named in
# ``source``. v4 rounds are also summarised here so the timeline reads end to end; their numbers come from the
# runs shown on the other tabs.
MILESTONES: list[dict[str, str]] = [
    {
        "date": "2026-09-20",
        "phase": "v2 预注册实验",
        "scope": "held-out 100 题，A/B 各跑 1 次",
        "result": "A 99/100，B 96/100；0 次违规；代码拦下危险提案 A 7 次、B 7 次；$0.44",
        "note": "B 在客户不知道编号的题上追问一次就放弃（3 次）；A 把越权请求答成退款状态（1 次）；B 越权题转人工，当时判错（1 次）。首跑还有 2 次 B 空回合导致接口报错，修适配器后整批重跑。",
        "source": "docs/EXPERIMENT.md",
    },
    {
        "date": "2026-09-20",
        "phase": "挑战集 v1",
        "scope": "8 道手写题，各 1 次",
        "result": "A 6/8，B 7/8",
        "note": "A：目标有歧义仍选了一笔、要求退差价只解释了订阅；B：追问失败后提前结束。",
        "source": "analysis/challenge-review/report.md",
    },
    {
        "date": "2026-09-20",
        "phase": "v3 / v3.1 迭代",
        "scope": "已知挑战 8 题、新分支 12 题、v2 定向复测 6 题",
        "result": "A 8/8、12/12、4/6；B 8/8、8/12、4/6",
        "note": "退款目标只认客户原话或全账户唯一多扣的付款；新增退差价语义字段；结论输入补上账户记录。B 余下的失败：协议修复耗尽、两处评分口径过窄、运行器 60 秒超时。",
        "source": "analysis/v3.1-iteration/report.md",
    },
    {
        "date": "2026-09-22",
        "phase": "v4 第一轮",
        "scope": "canary-v1、regression-v1、sealed-v1（48 题封存集）",
        "result": "门禁未通过：A 在封存集 S=0.701，B 0.886；0 安全事件；12 条必过回归全过",
        "note": "找到 A 的 4 个缺陷：F1 读取故障当成编号错误、F2 取消退款误判为查进度、F3 越权请求误路由、F4 编号冲突答成没有重复。",
        "source": "docs/EVALUATION_V4.md §4",
    },
    {
        "date": "2026-09-22",
        "phase": "v4 第二轮",
        "scope": "修复 F1–F4 后回归；中途 4 次被评测拦下的自身回归",
        "result": "canary-v2 A 154/154、B 145/154；regression A 45/45、B 39/45；sealed-v1 A S=0.951（已暴露，只算测量）",
        "note": "封存集的族级结果用于定位缺陷后转为已暴露；下一次门禁需要 sealed-v2。",
        "source": "docs/EVALUATION_V4.md §4.2",
    },
    {
        "date": "2026-09-26",
        "phase": "规范 v1.2",
        "scope": "补齐退款规则、撤回、查进度、范围外请求、写入异常；新增 fixes-v2 24 题",
        "result": "共有 42 题 A 154/154、B 150/154；新题 A 119/120、B 120/120；regression A 45/45、B 41/45；0 安全事件",
        "note": "回归中修了撤销已完成退款的抽取（v1.2b）和越权请求的代码兜底（v1.2c）；发现约一半中文题收到英文回复（老问题）。",
        "source": "docs/EVALUATION_V4.md §4.3",
    },
]


# -- helpers ----------------------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def is_sealed(task_id: Any, dataset: Any = None) -> bool:
    return str(task_id or "").startswith("sealed-") or str(dataset or "").startswith("sealed")


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def replied_in_english(text: str) -> bool:
    """A reply to a Chinese-language task with (almost) no Chinese in it."""
    compact = re.sub(r"\s", "", text)
    return bool(compact) and len(CJK.findall(text)) <= 0.2 * len(compact)


def _reply_text(row: dict[str, Any]) -> str:
    return str(((row.get("layers") or {}).get("response") or {}).get("conclusion_text") or "")


def _safety_event(row: dict[str, Any]) -> bool:
    return any(v is True for k, v in (row.get("safety") or {}).items() if k != "evidence")


def failed_checks(layers: dict[str, Any]) -> list[str]:
    out = []
    for layer in ("outcome", "behaviour"):
        block = layers.get(layer) or {}
        for name, value in (block.get("checks") or {}).items():
            if value is False:
                out.append(f"{layer}.{name}")
        if block.get("status") == "unknown":
            out.append(f"{layer}.unknown:{block.get('reason')}")
    return out


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def short_name(run_id: str) -> str:
    """This project's run naming: v4-* is round 1, v4r2X-* round 2X, v4v12X-* spec v1.2X."""
    if m := re.match(r"v4v12([a-z]?)-", run_id):
        return "v1.2" + m.group(1)
    if m := re.match(r"v4r2([a-z]?)-", run_id):
        return "r2" + m.group(1)
    if run_id.startswith("v4-"):
        return "r1"
    if run_id.startswith("calibration"):
        return "cal"
    return run_id


def _series(task_set: str) -> str:
    return re.sub(r"-v\d+(?:\.\d+)*$", "", task_set) or task_set


# -- runs -----------------------------------------------------------------------------------------


def suite_runs(runs_dir: Path) -> list[Path]:
    """Directories written by ``suite run`` (the v4 contract), including aborted ones."""
    found = []
    for manifest in sorted(runs_dir.glob("*/manifest.json")) + sorted(runs_dir.glob("aborted/*/manifest.json")):
        results = manifest.parent / "results.jsonl"
        if not results.exists() or not isinstance(_read_json(manifest), dict):
            continue
        with results.open(encoding="utf-8") as fh:
            first = fh.readline()
        if first.strip() and "layers" in json.loads(first):
            found.append(manifest.parent)
    return found


def _results(run: Path) -> tuple[list[dict[str, Any]], str]:
    rescored = run / "rescore-g2" / "results.jsonl"
    if rescored.exists():
        return _jsonl(rescored), "rescore-g2"
    # A provider outage marked after the run (``suite reclassify-outage``): its trials show as unknown, not as agent failures.
    reclassified = sorted(run.glob("reclass-infra-*/results.jsonl"), key=lambda p: int(p.parent.name.rsplit("-", 1)[1]))
    if reclassified:
        return _jsonl(reclassified[-1]), reclassified[-1].parent.name
    return _jsonl(run / "results.jsonl"), "as-run"


def _group_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_task: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        by_task[r["task_id"]].append(r.get("status") == "pass")  # unknown counts as a failure (S_lower)
    passed = sum(r.get("status") == "pass" for r in rows)
    unknown = sum(r.get("status") not in ("pass", "fail") for r in rows)
    costs = [float(r.get("cost_usd") or 0) for r in rows]
    latency = [float(r.get("latency_s") or 0) for r in rows]
    zh = [_reply_text(r) for r in rows if r.get("language") == "zh"]
    zh = [t for t in zh if t]
    return {
        "n": len(rows),
        "tasks": len(by_task),
        "pass": passed,
        "fail": len(rows) - passed - unknown,
        "unknown": unknown,
        "S": round(statistics.mean(statistics.mean(v) for v in by_task.values()), 4) if by_task else None,
        "safety": sum(_safety_event(r) for r in rows),
        "evidence_incomplete": sum(1 for r in rows if (r.get("safety") or {}).get("evidence") not in (None, "complete")),
        "own": sum(r.get("failure_class") == "own" for r in rows),
        "cost": round(sum(costs), 4),
        "p50": round(_percentile(latency, 0.5), 1),
        "p95": round(_percentile(latency, 0.95), 1),
        "repairs": sum(int(r.get("repairs") or 0) for r in rows),
        "repair_trials": sum(1 for r in rows if int(r.get("repairs") or 0) > 0),
        "clarifications": sum(int(r.get("clarifications") or 0) for r in rows),
        "zh": len(zh),
        "zh_en": sum(replied_in_english(t) for t in zh),
    }


def _judge_summary(run: Path) -> dict[str, Any] | None:
    path = run / "judge" / "judge.jsonl"
    if not path.exists():
        return None
    out: dict[str, dict[str, int]] = {}
    for row in _jsonl(path):
        g = out.setdefault(str(row.get("group")), {"pass": 0, "fail": 0, "unknown": 0, "severe": 0})
        status = row.get("status") if row.get("status") in ("pass", "fail") else "unknown"
        g[status] += 1
        g["severe"] += bool(row.get("severe"))
    return out


def _task_count(root: Path, tasks_path: str) -> int | None:
    data = _read_json(root / tasks_path) if tasks_path else None
    if isinstance(data, dict):
        data = data.get("tasks")
    return len(data) if isinstance(data, list) else None


def summarize_run(run: Path, root: Path) -> dict[str, Any]:
    manifest = _read_json(run / "manifest.json") or {}
    status = _read_json(run / "status.json") or {}
    rows, source = _results(run)
    run_id = run.name
    task_set = Path(str(manifest.get("tasks_path") or "")).stem or "?"
    full = _task_count(root, str(manifest.get("tasks_path") or ""))
    sealed = any(is_sealed(r.get("task_id"), r.get("dataset")) for r in rows)

    families: dict[str, dict[str, Any]] = {}
    slices: dict[str, dict[str, list[int]]] = defaultdict(dict)
    for r in rows:
        g = r.get("group")
        if g not in GROUPS:
            continue
        passed = int(r.get("status") == "pass")
        fam = families.setdefault(r.get("family") or r["task_id"], {"line": r.get("business_line"), "A": None, "B": None})
        cell = fam[g] = fam[g] or [0, 0]
        cell[0] += passed
        cell[1] += 1
        line = slices[str(r.get("business_line"))].setdefault(g, [0, 0])
        line[0] += passed
        line[1] += 1

    # Failures with the same task, group, end state and failed checks are one line with a count.
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in rows:
        if r.get("status") == "pass":
            continue
        checks = failed_checks(r.get("layers") or {})
        key = (r["task_id"], r.get("group"), r.get("status"), r.get("final_state"), r.get("conclusion_type"), tuple(checks), r.get("failure_class"))
        entry = grouped.setdefault(key, {
            "task": r["task_id"],
            "g": r.get("group"),
            "n": 0,
            "trials": [],
            "status": r.get("status"),
            "state": r.get("final_state"),
            "concl": r.get("conclusion_type"),
            "checks": checks,
            "fc": r.get("failure_class"),
            "artifact": None if is_sealed(r.get("task_id"), r.get("dataset")) else (r.get("artifact") or {}).get("path"),
        })
        entry["n"] += 1
        entry["trials"].append(r.get("trial_index"))
    for entry in grouped.values():
        entry["trials"].sort(key=lambda t: (t is None, t))
    failures = sorted(grouped.values(), key=lambda f: (-f["n"], str(f["task"]), str(f["g"])))

    spend = status.get("spend") or {}
    return {
        "id": run_id,
        "short": short_name(run_id),
        "dir": str(run.relative_to(root)),
        "label": manifest.get("label"),
        "started": manifest.get("started_at"),
        "finished": status.get("finished_at"),
        "task_set": task_set,
        "series": _series(task_set),
        "tasks": manifest.get("tasks"),
        "task_set_size": full,
        "subset": bool(full and manifest.get("tasks") and int(manifest["tasks"]) < full),
        "trials": manifest.get("trials"),
        "key_risk_trials": manifest.get("key_risk_trials"),
        "workers": manifest.get("workers"),
        "seed": manifest.get("seed"),
        "model": manifest.get("model"),
        "budget": manifest.get("budget_usd"),
        "spent": spend.get("spent_usd", sum(float(r.get("cost_usd") or 0) for r in rows)),
        "wall": status.get("wall_seconds"),
        "planned": status.get("planned"),
        "recorded": status.get("recorded", len(rows)),
        "complete": bool(status.get("complete")),
        "stopped": status.get("stopped_reason"),
        "aborted": run.parent.name == "aborted",
        "sealed": sealed,
        "source": source,
        "groups": {g: _group_stats([r for r in rows if r.get("group") == g]) for g in GROUPS if any(r.get("group") == g for r in rows)},
        "families": [[name, f["line"], f["A"], f["B"]] for name, f in sorted(families.items())],
        "slices": slices,
        "failures": failures,
        "judge": _judge_summary(run),
    }


# -- case desk --------------------------------------------------------------------------------------


def _trail(steps: list[Any]) -> str:
    out: list[list[Any]] = []
    for step in steps:
        if out and out[-1][0] == step:
            out[-1][1] += 1
        else:
            out.append([step, 1])
    return " → ".join(f"{s}×{n}" if n > 1 else str(s) for s, n in out[:40])


def _followup_text(item: Any) -> str:
    if isinstance(item, dict):
        return " · ".join(str(item[k]) for k in ("when", "command", "text") if item.get(k))
    return str(item)


def trial_detail(conn: sqlite3.Connection, trial_ids: list[str], root: Path) -> dict[str, Any] | None:
    for trial_id in trial_ids:
        row = conn.execute("SELECT run_dir, artifact_path, layers_json FROM eval_trials WHERE trial_id=?", (trial_id,)).fetchone()
        if row is None or not row["artifact_path"]:
            continue
        artifact = _read_json(root / row["run_dir"] / row["artifact_path"])
        if not isinstance(artifact, dict):
            continue
        task = artifact.get("task") or {}
        if is_sealed(task.get("task_id"), task.get("dataset")):
            return None
        view = artifact.get("view") or {}
        case = view.get("case") or {}
        snap = artifact.get("snapshot") or {}
        extracted = snap.get("extracted") or {}
        return {
            "trial": trial_id,
            "artifact": f"{row['run_dir']}/{row['artifact_path']}",
            "text": _clip(task.get("user_text"), 500),
            "followups": [_clip(_followup_text(x), 200) for x in (task.get("followups") or [])][:4],
            "reply": _clip(case.get("final_message"), 900),
            "state": case.get("state"),
            "concl": (case.get("conclusion") or {}).get("type"),
            "expected": [[o.get("conclusion"), o.get("final_state")] for o in ((task.get("expected") or {}).get("outcomes_any") or [])],
            "checks": failed_checks(json.loads(row["layers_json"] or "{}")),
            "evidence": [[e.get("tool"), _clip(json.dumps(e.get("args") or {}, ensure_ascii=False), 120), bool(e.get("ok"))] for e in (view.get("evidence") or [])][:12],
            "clarifications": [[_clip(c.get("question"), 240), _clip(c.get("answer"), 240)] for c in (view.get("clarifications") or [])][:3],
            "trail": _trail(view.get("trail") or snap.get("trail") or []),
            "flags": sorted(k for k, v in extracted.items() if v is True),
            "ids": list(extracted.get("invoice_ids") or []) + list(extracted.get("payment_ids") or []),
            "escalation": (case.get("escalation") or {}).get("reason"),
            "protocol_errors": len(snap.get("protocol_errors") or []),
            "fallback": bool(snap.get("extraction_fallback")),
        }
    return None


def case_desk(db: Path, root: Path) -> dict[str, Any]:
    if not db.exists():
        return {"findings": [], "counts": {}}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        counts = {kind: n for kind, n in conn.execute("SELECT kind, COUNT(*) FROM findings GROUP BY kind")}
        findings = []
        for f in conn.execute("SELECT * FROM findings WHERE kind IN ('badcase', 'dispute') ORDER BY created_at, finding_id"):
            task = str(f["task_id"] or "")
            sealed = is_sealed(task)
            trial_ids = json.loads(f["trial_ids_json"] or "[]")
            item: dict[str, Any] = {
                "id": f["finding_id"],
                "kind": f["kind"],
                "channel": f["channel"],
                "status": f["status"],
                "g": f["group_name"],
                "run": Path(str(f["run_dir"] or "")).name,
                "task": task.split("/")[1] if sealed and "/" in task else task,
                "sealed": sealed,
                "occ": f["occurrences"],
                "attr": f["attribution"],
                "created": f["created_at"],
                "updated": f["updated_at"],
                "trials": len(trial_ids),
            }
            if sealed:
                # Grader check names only; response-layer clusters carry rubric claim ids, which are sealed content.
                item["cluster"] = "" if str(f["channel"]).startswith("response") else f["cluster"]
            else:
                item["title"] = f["title"]
                item["cluster"] = f["cluster"]
                item["hypothesis"] = f["hypothesis"]
                item["detail"] = trial_detail(conn, trial_ids, root)
            findings.append(item)
        return {"findings": findings, "counts": counts}
    finally:
        conn.close()


# -- gates ----------------------------------------------------------------------------------------


def gate_reports(root: Path) -> list[dict[str, Any]]:
    out = []
    for path in sorted(root.glob("analysis/*/release.json")):
        d = _read_json(path)
        if not isinstance(d, dict) or "decision" not in d:
            continue
        regression = d.get("regression") or {}
        out.append({
            "path": str(path.relative_to(root)),
            "checked_at": d.get("checked_at"),
            "sealed_run": d.get("sealed_run"),
            "group": d.get("gate_group"),
            "decision": d.get("decision"),
            "reasons": d.get("reasons") or [],
            "S": d.get("S"),
            "slices": {k: (v or {}).get("s_lower") for k, v in (d.get("slices") or {}).items()},
            "regression_tasks": regression.get("tasks"),
            "regression_failing": len(regression.get("failing") or []),
            "thresholds": d.get("thresholds"),
        })
    return out


def sealed_sets(root: Path) -> list[dict[str, Any]]:
    manifest = _read_json(root / "data" / "assets_manifest.json") or {}
    return [
        {k: s.get(k) for k in ("path", "tasks", "families", "frozen_at", "exposure", "exposed_at")}
        for s in manifest.get("sealed") or []
    ]


# -- page -----------------------------------------------------------------------------------------


def collect(root: Path, *, runs_dir: Path | None = None, db: Path | None = None) -> dict[str, Any]:
    runs = [summarize_run(run, root) for run in suite_runs(runs_dir or root / "runs")]
    runs.sort(key=lambda r: str(r.get("started") or ""))
    desk = case_desk(db or root / "data" / "cases.sqlite3", root)
    return {
        "generated_at": utc_now(),
        "runs": runs,
        "findings": desk["findings"],
        "finding_counts": desk["counts"],
        "gates": gate_reports(root),
        "sealed_sets": sealed_sets(root),
        "milestones": MILESTONES,
    }


def _embed(data: dict[str, Any]) -> str:
    # Inside <script type="application/json">: no "<", ">" or "&" may close the element or start markup.
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    return raw.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


STANDALONE_HEAD = (
    "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
    "<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
    "body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n</head>\n<body>\n"
)


def render(data: dict[str, Any]) -> str:
    """The page without a document skeleton (the Artifact host adds its own)."""
    return TEMPLATE.read_text(encoding="utf-8").replace("__OBS_DATA__", _embed(data))


def build(root: Path, out_dir: Path) -> dict[str, Any]:
    data = collect(root)
    fragment = render(data)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "artifact.html").write_text(fragment, encoding="utf-8")
    (out_dir / "index.html").write_text(STANDALONE_HEAD + fragment + "\n</body>\n</html>\n", encoding="utf-8")
    return {
        "out": str(out_dir / "index.html"),
        "artifact": str(out_dir / "artifact.html"),
        "runs": len(data["runs"]),
        "trials": sum(g["n"] for r in data["runs"] for g in r["groups"].values()),
        "findings": len(data["findings"]),
        "bytes": len(fragment.encode("utf-8")),
    }
