"""The observability page: run summaries, case-desk detail, and nothing from a sealed task."""

from __future__ import annotations

import json
from pathlib import Path

from billingops.evaluation.dashboard import build, replied_in_english, short_name
from billingops.evaluation.findings import FindingsDB

TASKS = [
    {"task_id": "gold-v1/D01-named/zh-1", "dataset": "gold-v1", "family": "gold-D01", "business_line": "duplicate", "language": "zh"},
    {"task_id": "sealed-v1/D99-hidden/zh-1", "dataset": "sealed-v1", "family": "D99-hidden", "business_line": "duplicate", "language": "zh"},
]
TEXT = {"gold-v1/D01-named/zh-1": ("开发侧的客户消息", "Dev-side reply in English."), "sealed-v1/D99-hidden/zh-1": ("SEALED-SECRET-MESSAGE", "SEALED-SECRET-REPLY")}


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _project(root: Path) -> None:
    _write(root / "data" / "tasks" / "mini-v1.json", TASKS)
    run = root / "runs" / "v4v12-mini-v1"
    _write(run / "tasks.json", TASKS)
    _write(run / "manifest.json", {"label": "mini", "started_at": "2026-09-26T00:00:00+00:00", "tasks_path": "data/tasks/mini-v1.json", "task_set_sha256": "x",
                                   "tasks": 2, "groups": ["A", "B"], "trials": 1, "key_risk_trials": 1, "seed": 1, "workers": 1, "model": "fake", "budget_usd": None})
    _write(run / "status.json", {"finished_at": "2026-09-26T00:01:00+00:00", "planned": 4, "recorded": 4, "complete": True, "wall_seconds": 60, "spend": {"spent_usd": 0.01}})
    rows = []
    for task in TASKS:
        message, reply = TEXT[task["task_id"]]
        for group in ("A", "B"):
            passed = group == "B"
            name = f"artifacts/{task['family']}-{group}-t0.json"
            _write(run / name, {"task": {**task, "user_text": message, "expected": {"outcomes_any": [{"conclusion": "refund_proposed", "final_state": "awaiting_approval"}]}},
                                "view": {"case": {"state": "closed", "conclusion": {"type": "no_refund_due"}, "final_message": reply}, "evidence": [{"tool": "get_invoice", "args": {"invoice_id": "INV-1"}, "ok": True}], "trail": ["extract", "fixed", "fixed", "conclude"]},
                                "snapshot": {"extracted": {"mentions_duplicate": True, "invoice_ids": ["INV-1"]}}})
            rows.append({"trial_id": f"{task['task_id']}::{group}::0::abc", "task_id": task["task_id"], "dataset": task["dataset"], "family": task["family"],
                         "business_line": "duplicate", "language": "zh", "group": group, "trial_index": 0, "status": "pass" if passed else "fail",
                         "final_state": "awaiting_approval" if passed else "closed", "conclusion_type": "refund_proposed" if passed else "no_refund_due",
                         "cost_usd": 0.001, "latency_s": 5.0, "safety": {"unauthorized_refund": False, "evidence": "complete"},
                         "layers": {"outcome": {"status": "pass" if passed else "fail", "checks": {"outcome_allowed": passed}}, "behaviour": {"status": "pass", "checks": {}},
                                    "response": {"conclusion_text": reply}},
                         "artifact": {"path": name}})
    (run / "results.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    desk = FindingsDB(root / "data" / "cases.sqlite3")
    desk.import_run(run)
    desk.mine(run)


def _embedded(page: str) -> dict:
    start = page.index('id="obs-data">') + len('id="obs-data">')
    return json.loads(page[start:page.index("</script>", start)])


def test_the_page_summarises_runs_and_never_carries_sealed_text(tmp_path: Path) -> None:
    _project(tmp_path)
    result = build(tmp_path, tmp_path / "out")
    page = (tmp_path / "out" / "artifact.html").read_text(encoding="utf-8")
    assert "SEALED-SECRET" not in page
    assert page.startswith("<title>") and "<!doctype" not in page.lower()
    assert (tmp_path / "out" / "index.html").read_text(encoding="utf-8").lower().startswith("<!doctype html>")

    data = _embedded(page)
    [run] = data["runs"]
    assert (run["short"], run["series"], run["subset"]) == ("v1.2", "mini", False)
    assert run["groups"]["A"]["pass"] == 0 and run["groups"]["B"]["pass"] == 2
    assert run["groups"]["A"]["zh_en"] == 2  # both A replies are English answers to Chinese tasks
    sealed_failure = next(f for f in run["failures"] if f["task"].startswith("sealed"))
    assert sealed_failure["artifact"] is None

    sealed = [f for f in data["findings"] if f["sealed"]]
    dev = [f for f in data["findings"] if not f["sealed"] and f["kind"] == "badcase"]
    assert sealed and all("detail" not in f and "title" not in f for f in sealed)
    assert sealed[0]["task"] == "D99-hidden"
    assert dev and dev[0]["detail"]["text"] == "开发侧的客户消息"
    assert dev[0]["detail"]["trail"] == "extract → fixed×2 → conclude"
    assert result["runs"] == 1 and result["findings"] == len(data["findings"])


def test_embedded_data_cannot_close_its_script_element(tmp_path: Path) -> None:
    _project(tmp_path)
    run = tmp_path / "runs" / "v4v12-mini-v1"
    art = run / "artifacts" / "gold-D01-A-t0.json"
    content = json.loads(art.read_text(encoding="utf-8"))
    content["task"]["user_text"] = "</script><script>alert(1)</script>"
    art.write_text(json.dumps(content), encoding="utf-8")
    build(tmp_path, tmp_path / "out")
    page = (tmp_path / "out" / "artifact.html").read_text(encoding="utf-8")
    assert "alert(1)" in page and "<script>alert(1)" not in page
    assert _embedded(page)["findings"]


def test_helpers() -> None:
    assert short_name("v4-canary-v1-deepseek-flash") == "r1"
    assert short_name("v4r2e-canary-v2") == "r2e"
    assert short_name("v4v12b-s13") == "v1.2b"
    assert replied_in_english("The refund is completed.") and not replied_in_english("退款已完成（RF-0001，49.00 元）。")


def test_a_reclassified_outage_is_shown_instead_of_the_as_run_rows(tmp_path: Path) -> None:
    from billingops.evaluation.dashboard import _results

    run = tmp_path / "run"
    row = {"task_id": "t", "group": "B", "status": "fail", "failure_class": "unknown"}
    (run / "reclass-infra-1").mkdir(parents=True)
    (run / "results.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert _results(run)[1] == "as-run"
    (run / "reclass-infra-1" / "results.jsonl").write_text(json.dumps({**row, "status": "unknown", "failure_class": "infra_model"}) + "\n", encoding="utf-8")
    rows, source = _results(run)
    assert source == "reclass-infra-1" and rows[0]["status"] == "unknown"
