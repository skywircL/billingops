"""Repeated, isolated, budgeted execution of a task set: the run protocol.

    suite run --tasks data/tasks/gold-v1.json --groups A,B --trials 3 --model openai --workers 2 --budget-usd 5

* every (task, group, trial) is one :func:`trials.run_trial` with a fresh
  application database and a reloaded Billing world;
* each worker process owns its own Billing service, so worlds never bleed
  between concurrent trials;
* the order interleaves A and B per task by a frozen seed, so endpoint drift
  over the wall clock hits both groups alike;
* spend is reserved per model call in one SQLite ledger shared by all workers
  and refused once the budget would be exceeded; the run then stops and marks
  itself incomplete;
* after ``PROVIDER_STOP_AFTER`` consecutive trials the model provider could
  not serve (``infra_model``: balance, quota, outage) the run stops as well,
  instead of recording every remaining trial as unknown;
* ``results.jsonl`` is appended per finished trial and a rerun with the same
  ``--out`` resumes, skipping trials already recorded.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..config import MODEL_ID, MODEL_MAX_TOKENS, MODEL_PRICE_INPUT_PER_M, MODEL_PRICE_OUTPUT_PER_M, MODEL_TEMPERATURE
from .artifacts import digest, source_manifest, write_json
from .tasks import CONTRACT_VERSION, EvalTask, load_tasks, utc_now
from .trials import KEY_RISKS, BudgetExhausted, run_trial

GROUPS = {"A": "A", "B": "B", "fixed": "A", "dynamic": "B"}

PROVIDER_STOP_AFTER = 5
"""Consecutive ``infra_model`` trials before the suite stops: an exhausted balance fails every trial after it."""


# -- spend ledger --------------------------------------------------------------------------


class SpendLedger:
    """Per-call reservations in SQLite. ``BEGIN IMMEDIATE`` serialises workers."""

    def __init__(self, path: Path, budget_usd: float) -> None:
        self.path = Path(path)
        self.budget_usd = float(budget_usd)
        conn = self._conn()
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS reservations (id INTEGER PRIMARY KEY AUTOINCREMENT, est REAL NOT NULL, actual REAL, at TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        )
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('budget_usd', ?)", (str(self.budget_usd),))
        conn.commit()
        conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def reserve(self, est: float) -> int | None:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            spent, open_ = conn.execute("SELECT COALESCE(SUM(actual),0), COALESCE(SUM(CASE WHEN actual IS NULL THEN est ELSE 0 END),0) FROM reservations").fetchone()
            if spent + open_ + est > self.budget_usd:
                conn.execute("ROLLBACK")
                return None
            cur = conn.execute("INSERT INTO reservations (est, actual, at) VALUES (?, NULL, ?)", (float(est), utc_now()))
            conn.execute("COMMIT")
            return int(cur.lastrowid)
        finally:
            conn.close()

    def settle(self, reservation_id: int, actual: float) -> None:
        conn = self._conn()
        try:
            conn.execute("UPDATE reservations SET actual=? WHERE id=?", (float(actual), int(reservation_id)))
        finally:
            conn.close()

    def status(self) -> dict[str, float]:
        conn = self._conn()
        try:
            spent, open_, n = conn.execute("SELECT COALESCE(SUM(actual),0), COALESCE(SUM(CASE WHEN actual IS NULL THEN est ELSE 0 END),0), COUNT(*) FROM reservations").fetchone()
        finally:
            conn.close()
        return {"budget_usd": self.budget_usd, "spent_usd": float(spent), "reserved_open_usd": float(open_), "calls": int(n), "remaining_usd": self.budget_usd - float(spent) - float(open_)}


# -- planning --------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrialSlot:
    task_id: str
    group: str
    trial_index: int

    @property
    def key(self) -> str:
        return f"{self.task_id}|{self.group}|{self.trial_index}"


def plan(tasks: Iterable[EvalTask], *, groups: tuple[str, ...], trials: int, key_risk_trials: int, seed: int) -> list[TrialSlot]:
    """Task order shuffled by seed; within a task, group order alternates per trial index."""
    rng = random.Random(seed)
    ordered = list(tasks)
    rng.shuffle(ordered)
    slots: list[TrialSlot] = []
    for t in ordered:
        n = key_risk_trials if any(r in KEY_RISKS for r in t.risk) else trials
        for i in range(n):
            order = list(groups) if i % 2 == 0 else list(reversed(groups))
            for g in order:
                slots.append(TrialSlot(t.task_id, g, i))
    return slots


# -- worker ----------------------------------------------------------------------------------

_WORKER: dict[str, Any] = {}


def _init_worker() -> None:
    from ..billing_client import BillingClient
    from ..testing import BillingServer

    tmp = tempfile.mkdtemp(prefix=f"billingops-suite-w{os.getpid()}-")
    server = BillingServer(Path(tmp) / "billing.sqlite3").start()
    _WORKER["server"] = server
    _WORKER["billing"] = BillingClient(server.url, admin_token="demo-admin")


def _run_slot(task_raw: dict[str, Any], group: str, trial_index: int, workdir: str, ledger_path: str | None, budget: float, model_spec: str, versions: dict[str, Any]) -> dict[str, Any]:
    if "billing" not in _WORKER:
        _init_worker()
    task = EvalTask.from_dict(task_raw)
    ledger = SpendLedger(Path(ledger_path), budget) if ledger_path else None
    result = run_trial(task, group, model_spec=model_spec, billing=_WORKER["billing"], workdir=Path(workdir), trial_index=trial_index, ledger=ledger, versions=versions)
    return result.to_dict()


# -- the suite ---------------------------------------------------------------------------------


def run_suite(
    *,
    tasks_path: Path,
    out_dir: Path,
    groups: tuple[str, ...] = ("A", "B"),
    trials: int = 3,
    key_risk_trials: int = 7,
    model_spec: str = "fake",
    workers: int = 1,
    budget_usd: float | None = None,
    seed: int = 20260921,
    label: str | None = None,
    limit: int | None = None,
    task_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    tasks = load_tasks(tasks_path)
    if task_ids:
        wanted = set(task_ids)
        tasks = [t for t in tasks if t.task_id in wanted]
        missing = wanted - {t.task_id for t in tasks}
        if missing:
            raise ValueError(f"unknown task ids: {sorted(missing)}")
    if limit:
        tasks = tasks[:limit]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    manifest_path = out_dir / "manifest.json"
    workdir = out_dir / "dbs"
    workdir.mkdir(exist_ok=True)
    groups = tuple(GROUPS[g] for g in groups)
    versions = {"contract": CONTRACT_VERSION, "model": MODEL_ID if model_spec == "openai" else model_spec, "task_set_sha256": digest([t.to_dict() for t in tasks])}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["task_set_sha256"] != versions["task_set_sha256"] or manifest["groups"] != list(groups) or manifest["trials"] != trials:
            raise ValueError("resume refused: task set, groups or trial count differ from the existing manifest")
    else:
        manifest = {
            "label": label or f"{tasks_path.stem}-{model_spec}",
            "started_at": utc_now(),
            "tasks_path": str(tasks_path),
            "task_set_sha256": versions["task_set_sha256"],
            "tasks": len(tasks),
            "groups": list(groups),
            "trials": trials,
            "key_risk_trials": key_risk_trials,
            "seed": seed,
            "workers": workers,
            "model": versions["model"],
            "model_spec": model_spec,
            "prices_usd_per_m": {"input": MODEL_PRICE_INPUT_PER_M, "output": MODEL_PRICE_OUTPUT_PER_M},
            "model_config": {"model": MODEL_ID, "temperature": MODEL_TEMPERATURE, "max_tokens": MODEL_MAX_TOKENS},
            "budget_usd": budget_usd,
            "provenance": source_manifest(),
            "note": "scripted stand-in: harness validation only" if model_spec != "openai" else "live model; no local reuse or replay",
        }
        write_json(manifest_path, manifest)
        write_json(out_dir / "tasks.json", [t.to_dict() for t in tasks])

    done: set[str] = set()
    if results_path.exists():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                done.add(f"{r['task_id']}|{r['group']}|{r['trial_index']}")
    slots = [s for s in plan(tasks, groups=groups, trials=trials, key_risk_trials=key_risk_trials, seed=seed) if s.key not in done]
    by_id = {t.task_id: t for t in tasks}
    ledger_path = out_dir / "spend.sqlite3" if budget_usd is not None and model_spec == "openai" else None
    ledger = SpendLedger(ledger_path, budget_usd) if ledger_path else None

    started = time.monotonic()
    stopped_reason: str | None = None
    finished = 0
    infra_streak = 0
    with results_path.open("a", encoding="utf-8") as fh:
        if workers <= 1:
            _init_worker()
            try:
                for slot in slots:
                    row = _run_slot(by_id[slot.task_id].to_dict(), slot.group, slot.trial_index, str(workdir), str(ledger_path) if ledger_path else None, budget_usd or 0.0, model_spec, versions)
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fh.flush()
                    finished += 1
                    _print(row, finished, len(slots))
                    infra_streak = infra_streak + 1 if row.get("failure_class") == "infra_model" else 0
                    if row.get("failure_class") == "budget":
                        stopped_reason = "budget_exhausted"
                        break
                    if infra_streak >= PROVIDER_STOP_AFTER:
                        stopped_reason = "provider_unavailable"
                        break
            finally:
                _WORKER["billing"].close()
                _WORKER["server"].stop()
                _WORKER.clear()
        else:
            with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as pool:
                pending = {}
                it = iter(slots)
                for slot in it:
                    fut = pool.submit(_run_slot, by_id[slot.task_id].to_dict(), slot.group, slot.trial_index, str(workdir), str(ledger_path) if ledger_path else None, budget_usd or 0.0, model_spec, versions)
                    pending[fut] = slot
                    if len(pending) >= workers:
                        break
                while pending:
                    for fut in as_completed(list(pending)):
                        pending.pop(fut)
                        row = fut.result()
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                        fh.flush()
                        finished += 1
                        _print(row, finished, len(slots))
                        infra_streak = infra_streak + 1 if row.get("failure_class") == "infra_model" else 0
                        if row.get("failure_class") == "budget":
                            stopped_reason = "budget_exhausted"
                        elif infra_streak >= PROVIDER_STOP_AFTER:
                            stopped_reason = "provider_unavailable"
                        elif stopped_reason is None:
                            nxt = next(it, None)
                            if nxt is not None:
                                pending[pool.submit(_run_slot, by_id[nxt.task_id].to_dict(), nxt.group, nxt.trial_index, str(workdir), str(ledger_path) if ledger_path else None, budget_usd or 0.0, model_spec, versions)] = nxt
                        break
    status = {
        "finished_at": utc_now(),
        "planned": len(slots) + len(done),
        "recorded": finished + len(done),
        "complete": stopped_reason is None and finished == len(slots),
        "stopped_reason": stopped_reason,
        "wall_seconds": time.monotonic() - started,
        "spend": ledger.status() if ledger else None,
    }
    # status.json is the run's live state and is rewritten on resume; every version is kept in the history log.
    (out_dir / "status.json").write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    with (out_dir / "status-history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(status, ensure_ascii=False) + "\n")
    return status


def _print(row: Mapping[str, Any], n: int, total: int) -> None:
    mark = {"pass": "ok  ", "fail": "FAIL", "unknown": "??  "}.get(str(row.get("status")), "    ")
    print(f"[{n:4d}/{total}] {row['group']} t{row['trial_index']} {mark} {row['task_id'][:48]:48s} {row['final_state']:12s} {str(row.get('conclusion_type') or '-')[:22]:22s} {row.get('failure_class') or ''} ${row.get('cost_usd', 0):.4f} {row.get('latency_s', 0):.1f}s", flush=True)
