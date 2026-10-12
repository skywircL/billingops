"""Immutable trial bundles and read-only access to pre-bundle experiments."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

ROOT = Path(__file__).resolve().parents[3]


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")


def source_manifest(*, historical: bool = False) -> dict[str, Any]:
    from ..config import MODEL_ID, MODEL_MAX_TOKENS, MODEL_TEMPERATURE
    from ..tools import EXTRACT_TOOL_SCHEMA, INVESTIGATION_TOOL_SCHEMAS

    files = sorted((ROOT / "src").rglob("*.py")) + [ROOT / "pyproject.toml", ROOT / "uv.lock"]
    return {
        "captured_at": utc_now(),
        "provenance": "audit_time_only_not_historical_freeze" if historical else "before_execution",
        "source_digests": {str(p.relative_to(ROOT)): file_digest(p) for p in files if p.exists()},
        "tool_schemas_sha256": digest([EXTRACT_TOOL_SCHEMA, INVESTIGATION_TOOL_SCHEMAS]),
        "model_config": {"model": MODEL_ID, "temperature": MODEL_TEMPERATURE, "max_tokens": MODEL_MAX_TOKENS},
    }


def capture_trial(*, case: Any, group: str, runtime: Any, view: dict | None, billing: Any, result_dir: Path) -> dict[str, Any]:
    """Capture before the next load_world. Export failures never rewrite the run outcome."""
    errors: list[str] = []
    world = None
    try:
        world = billing.dump_world()
    except Exception as exc:
        errors.append(f"billing_final:{type(exc).__name__}")
    app_case_id = (view or {}).get("case", {}).get("id")
    snapshot, events, tasks = {}, [], []
    if app_case_id:
        try:
            snapshot = runtime._snapshot(app_case_id)
            events = runtime.store.events(app_case_id)
            tasks = runtime.store.tasks(app_case_id)
        except Exception as exc:
            errors.append(f"application_capture:{type(exc).__name__}")
    data = {
        "schema_version": 1, "captured_at": utc_now(), "case_id": case.case_id, "group": group,
        "app_case_id": app_case_id, "task": case.to_dict(), "view": view, "snapshot": snapshot,
        "events": events, "tasks": tasks, "billing_final": world, "errors": errors,
        "capture_scope": "CaseRuntime + live Billing HTTP; not FastAPI ingress latency",
    }
    data["content_digests"] = {k: digest(data[k]) for k in ("task", "view", "snapshot", "events", "tasks", "billing_final")}
    path = result_dir / "artifacts" / f"{case.case_id}-{group}.json"
    try:
        write_json(path, data)
        sha = file_digest(path)
    except Exception as exc:
        errors.append(f"artifact_write:{type(exc).__name__}")
        sha = None
    return {"path": str(path.relative_to(result_dir)), "sha256": sha, "errors": errors}


def load_results(run_dir: Path) -> list[dict[str, Any]]:
    rows = [json.loads(s) for s in (run_dir / "results.jsonl").read_text().splitlines() if s.strip()]
    ids = [(r["case_id"], r["group"]) for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate (case_id, group) in results")
    return rows


def task_index(path: Path) -> dict[str, dict[str, Any]]:
    return {r["case_id"]: r for r in json.loads(path.read_text())}


def read_legacy_db(path: Path) -> dict[str, Any]:
    """Never instantiate CaseRuntime: it runs schema setup and can acquire locks."""
    if not path.is_file():
        raise FileNotFoundError(path)
    if Path(str(path) + "-wal").exists():
        raise ValueError("active WAL: export a consistent closed database before review")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    db.row_factory = sqlite3.Row
    try:
        cases = db.execute("SELECT * FROM cases").fetchall()
        if len(cases) != 1:
            raise ValueError("expected one case per trial database")
        case = dict(cases[0])
        for k in ("proposal_json", "conclusion_json", "escalation_json"):
            case[k[:-5]] = json.loads(case.pop(k)) if case.get(k) else None
        cp = db.execute("SELECT type, checkpoint FROM checkpoints WHERE thread_id=? ORDER BY checkpoint_id DESC LIMIT 1", (case["id"],)).fetchone()
        if cp is None:
            raise ValueError("missing checkpoint")
        snapshot = JsonPlusSerializer().loads_typed((cp["type"], cp["checkpoint"]))["channel_values"]
        events = []
        for row in db.execute("SELECT * FROM events WHERE case_id=? ORDER BY seq", (case["id"],)):
            event = dict(row)
            event["payload"] = json.loads(event.pop("payload_json"))
            events.append(event)
        tasks = []
        for row in db.execute("SELECT * FROM tasks WHERE case_id=? ORDER BY id", (case["id"],)):
            task = dict(row)
            task["payload"] = json.loads(task.pop("payload_json"))
            tasks.append(task)
        effect = db.execute("SELECT payload FROM effects WHERE key=?", (case.get("action_key"),)).fetchone()
        execution = json.loads(effect[0]) if effect else None
        view = {"case": case, "execution": execution}
        for key in ("evidence", "usage", "clarifications", "protocol_errors", "blocked_proposals", "invalidated_proposals", "trail"):
            view[key] = snapshot.get(key, [])
        view["budgets"] = snapshot.get("budgets", {})
        return {"view": view, "snapshot": snapshot, "events": events, "tasks": tasks, "app_case_id": case["id"]}
    finally:
        db.close()


def load_trial(run_dir: Path, row: dict[str, Any], tasks: dict[str, dict]) -> dict[str, Any]:
    trial_id = f"{row['case_id']}-{row['group']}"
    artifact = run_dir / "artifacts" / f"{trial_id}.json"
    if artifact.exists():
        data = json.loads(artifact.read_text())
        expected = row.get("artifact", {}).get("sha256")
        if expected and file_digest(artifact) != expected:
            raise ValueError("trial artifact digest mismatch")
        for k, sha in data["content_digests"].items():
            if digest(data[k]) != sha:
                raise ValueError(f"artifact content mismatch: {k}")
        if data["case_id"] != row["case_id"] or data["group"] != row["group"]:
            raise ValueError("artifact trial identity mismatch")
        data["task_provenance"] = "captured_at_execution"
    else:
        data = read_legacy_db(run_dir / "dbs" / f"{trial_id}.sqlite3")
        data.update(task=tasks.get(row["case_id"]), billing_final=None, errors=["billing_final_not_archived"], task_provenance="current_dataset_not_historical_freeze")
        if data["task"] and data["task"]["user_text"] != data["view"]["case"]["text"]:
            raise ValueError("task input does not match stored case")
    data.update(trial_id=trial_id, original_result=row, source_run=run_dir.name, source_db=str(run_dir / "dbs" / f"{trial_id}.sqlite3"))
    data["trace_runs"] = [e["payload"] for e in data.get("events", []) if e["kind"] == "observation_started"]
    return data


def new_output(run_dir: Path, output: Path | None, prefix: str) -> Path:
    path = output or ROOT / "analysis" / f"{run_dir.name}-{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    if path.resolve() == run_dir.resolve() or run_dir.resolve() in path.resolve().parents:
        raise ValueError("analysis output must be outside the original run directory")
    path.mkdir(parents=True, exist_ok=False)
    return path
