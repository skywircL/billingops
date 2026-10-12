"""The case flywheel: tasks, trials and findings in one SQLite database.

Three entities, deliberately separate:

* ``eval_tasks``   — a task definition and its version (imported from a run's tasks.json);
* ``eval_trials``  — one execution with its scores, costs, evidence pointers and version bundle;
* ``findings``     — one badcase / goodcase / dispute discovered from one or more trials, with
                     its review history. A task may accumulate findings across versions; a fix
                     never deletes the failure that motivated it.

Collection rules (``mine``): every failure, safety flag, A/B difference and severe response
finding is collected; successes are sampled at 10% per business line x language with a
seeded draw whose probability is stored on the finding, plus targeted mining of stable
successes and legal alternative paths, kept apart from the random sample so mining
frequency is never mistaken for a population rate. Imports are idempotent per trial id.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .tasks import utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_tasks (
    task_id TEXT NOT NULL, task_sha256 TEXT NOT NULL, dataset TEXT, family TEXT, business_line TEXT, language TEXT,
    mechanisms_json TEXT, exposure TEXT, versions_json TEXT, first_seen TEXT NOT NULL,
    PRIMARY KEY (task_id, task_sha256)
);
CREATE TABLE IF NOT EXISTS eval_trials (
    trial_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, task_sha256 TEXT NOT NULL, run_dir TEXT NOT NULL, group_name TEXT NOT NULL,
    trial_index INTEGER NOT NULL, status TEXT NOT NULL, failure_class TEXT, final_state TEXT, conclusion_type TEXT,
    cost_usd REAL, latency_s REAL, reads INTEGER, model_calls INTEGER, safety_json TEXT, layers_json TEXT, versions_json TEXT,
    artifact_path TEXT, artifact_sha256 TEXT, judge_json TEXT, trace_ids_json TEXT, langfuse_status TEXT, imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    finding_id TEXT PRIMARY KEY, kind TEXT NOT NULL, channel TEXT NOT NULL, task_id TEXT NOT NULL, run_dir TEXT NOT NULL,
    group_name TEXT, trial_ids_json TEXT NOT NULL, occurrences INTEGER NOT NULL, title TEXT NOT NULL, signature TEXT NOT NULL,
    first_deviation TEXT, hypothesis TEXT, confidence TEXT, attribution TEXT, impact TEXT, recoverable TEXT, cluster TEXT,
    status TEXT NOT NULL, business_correct INTEGER, expression_ok INTEGER, sampling_prob REAL, evidence_json TEXT,
    review_json TEXT NOT NULL, links_json TEXT NOT NULL, versions_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, finding_id TEXT, payload_json TEXT
);
CREATE TABLE IF NOT EXISTS imports (
    run_dir TEXT NOT NULL, imported_at TEXT NOT NULL, trials INTEGER NOT NULL, new_trials INTEGER NOT NULL, findings_created INTEGER NOT NULL DEFAULT 0
);
"""

KINDS = ("badcase", "goodcase", "dispute")
STATUSES = ("open", "reviewing", "confirmed", "fixed", "wontfix", "promoted", "not_a_defect")
ATTRIBUTIONS = ("agent", "tool", "environment", "simulator", "task", "grader", "unknown")
SAFETY_KEYS = ("unauthorized_refund", "wrong_target_refund", "over_refund", "duplicate_write", "cross_customer")


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


class FindingsDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- import ----------------------------------------------------------------------------------

    def import_run(self, run_dir: Path, *, results_name: str | None = None) -> dict[str, Any]:
        """``results_name`` imports a rescore (``<run>/<name>/results.jsonl``); trial ids stay the same, so a
        re-import under a new grader replaces nothing — the first imported score of a trial is kept and the
        rescore is recorded on the trial as ``layers``/``status`` only if the trial was not seen before."""
        run_dir = Path(run_dir)
        tasks = json.loads((run_dir / "tasks.json").read_text(encoding="utf-8"))
        base = run_dir / results_name if results_name else run_dir
        rows = [json.loads(l) for l in (base / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        judged: dict[str, Any] = {}
        jp = run_dir / "judge" / "judge.jsonl"
        if jp.exists():
            for line in jp.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    j = json.loads(line)
                    judged[j["trial_id"]] = j
        now = utc_now()
        shas = {}
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for t in tasks:
                sha = _digest(t)
                shas[t["task_id"]] = sha
                self.conn.execute(
                    "INSERT OR IGNORE INTO eval_tasks VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (t["task_id"], sha, t.get("dataset"), t.get("family"), t.get("business_line"), t.get("language"), json.dumps(t.get("mechanisms")), (t.get("exposure") or {}).get("status"), json.dumps(t.get("versions")), now),
                )
            new = 0
            for r in rows:
                trace_ids = _trace_ids(run_dir, r)
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO eval_trials VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["trial_id"], r["task_id"], shas.get(r["task_id"], ""), str(run_dir), r["group"], r["trial_index"], r["status"], r.get("failure_class"), r.get("final_state"), r.get("conclusion_type"),
                     r.get("cost_usd"), r.get("latency_s"), r.get("reads"), r.get("model_calls"), json.dumps(r.get("safety")), json.dumps(r.get("layers")), json.dumps(r.get("versions")),
                     (r.get("artifact") or {}).get("path"), (r.get("artifact") or {}).get("sha256"), json.dumps(judged.get(r["trial_id"])), json.dumps(trace_ids), "pending" if trace_ids else "none", now),
                )
                new += cur.rowcount
                if judged.get(r["trial_id"]) is not None:
                    self.conn.execute("UPDATE eval_trials SET judge_json=? WHERE trial_id=?", (json.dumps(judged[r["trial_id"]]), r["trial_id"]))
            self.conn.execute("INSERT INTO imports VALUES (?,?,?,?,0)", (str(run_dir), now, len(rows), new))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"run_dir": str(run_dir), "trials": len(rows), "new_trials": new, "tasks": len(tasks)}

    # -- mining ------------------------------------------------------------------------------------

    def mine(self, run_dir: Path, *, seed: int = 20260921, sample_rate: float = 0.10, actor: str = "miner") -> dict[str, Any]:
        run_dir = str(run_dir)
        trials = [dict(r) for r in self.conn.execute("SELECT * FROM eval_trials WHERE run_dir=?", (run_dir,))]
        tasks = {r["task_id"]: dict(r) for r in self.conn.execute("SELECT * FROM eval_tasks")}
        created = 0
        now = utc_now()
        by_task_group: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for t in trials:
            by_task_group[(t["task_id"], t["group_name"])].append(t)

        def add(finding: dict[str, Any]) -> None:
            nonlocal created
            fid = "f-" + _digest({k: finding[k] for k in ("kind", "channel", "task_id", "run_dir", "group_name", "signature")})[:16]
            existing = self.conn.execute("SELECT finding_id FROM findings WHERE finding_id=?", (fid,)).fetchone()
            if existing:
                return
            self.conn.execute(
                "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, finding["kind"], finding["channel"], finding["task_id"], finding["run_dir"], finding.get("group_name"), json.dumps(finding["trial_ids"]), len(finding["trial_ids"]),
                 finding["title"], finding["signature"], finding.get("first_deviation"), finding.get("hypothesis"), finding.get("confidence", "low"), finding.get("attribution", "unknown"),
                 finding.get("impact"), finding.get("recoverable"), finding.get("cluster"), "open", finding.get("business_correct"), finding.get("expression_ok"), finding.get("sampling_prob"),
                 json.dumps(finding.get("evidence", {}), ensure_ascii=False), json.dumps([{"by": actor, "at": now, "action": "mined", "note": finding["channel"]}]), json.dumps({}), json.dumps(finding.get("versions")), now, now),
            )
            self.conn.execute("INSERT INTO audit (at, actor, action, finding_id, payload_json) VALUES (?,?,?,?,?)", (now, actor, "mined", fid, json.dumps({"channel": finding["channel"]})))
            created += 1

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # 1. Failures, clustered by mechanism signature within (task, group).
            for (task_id, group), rows in by_task_group.items():
                fails = [r for r in rows if r["status"] == "fail"]
                clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for r in fails:
                    layers = json.loads(r["layers_json"] or "{}")
                    failed_checks = sorted(f"{layer}.{k}" for layer in ("outcome", "behaviour") for k, v in (layers.get(layer, {}).get("checks") or {}).items() if v is False)
                    sig = json.dumps({"checks": failed_checks, "failure_class": r["failure_class"], "conclusion": r["conclusion_type"], "state": r["final_state"]}, sort_keys=True)
                    clusters[sig].append(r)
                for sig, members in clusters.items():
                    s = json.loads(sig)
                    add({
                        "kind": "badcase", "channel": "auto_failure", "task_id": task_id, "run_dir": run_dir, "group_name": group, "signature": sig,
                        "trial_ids": [m["trial_id"] for m in members], "title": f"{group} {task_id}: {', '.join(s['checks']) or s['failure_class'] or 'fail'}",
                        "first_deviation": _first_deviation(run_dir, members[0]), "confidence": "low",
                        "attribution": "environment" if s["failure_class"] in ("infra_billing", "infra_model") else "unknown" if s["failure_class"] == "runner_limit" else "agent",
                        "cluster": ",".join(s["checks"]) or str(s["failure_class"]), "business_correct": 0,
                        "impact": "trial failed" + (f" ({len(members)}/{len(rows)} trials)"), "recoverable": "unknown",
                        "evidence": {"failed_checks": s["checks"], "failure_class": s["failure_class"], "conclusion": s["conclusion"], "final_state": s["state"], "artifacts": [m["artifact_path"] for m in members]},
                        "versions": json.loads(members[0]["versions_json"] or "{}"),
                    })
            # 2. Safety flags: one finding per trial, never clustered away.
            for r in trials:
                flags = [k for k in SAFETY_KEYS if (json.loads(r["safety_json"] or "{}")).get(k)]
                if flags:
                    add({"kind": "badcase", "channel": "safety", "task_id": r["task_id"], "run_dir": run_dir, "group_name": r["group_name"], "signature": r["trial_id"],
                         "trial_ids": [r["trial_id"]], "title": f"SAFETY {r['group_name']} {r['task_id']}: {', '.join(flags)}", "confidence": "high", "attribution": "agent",
                         "impact": "gate blocker", "recoverable": "no", "cluster": "safety:" + ",".join(flags), "business_correct": 0, "evidence": {"flags": flags, "artifact": r["artifact_path"]}})
            # 3. Response-layer findings from the judge (business may still be correct).
            for r in trials:
                j = json.loads(r["judge_json"] or "null")
                if j and j.get("status") == "fail":
                    bad = [c for c in j.get("claims", []) if (c["polarity"] == "must" and c["verdict"] == "false") or (c["polarity"] == "must_not" and c["verdict"] == "true")]
                    add({"kind": "badcase", "channel": "response_severe" if j.get("severe") else "response", "task_id": r["task_id"], "run_dir": run_dir, "group_name": r["group_name"],
                         "signature": json.dumps(sorted(c["id"] for c in bad)), "trial_ids": [r["trial_id"]], "title": f"{r['group_name']} {r['task_id']}: reply fails {', '.join(c['id'] for c in bad)}",
                         "confidence": "low", "attribution": "unknown", "impact": "expression", "recoverable": "n/a", "cluster": "response:" + ",".join(sorted(c["id"] for c in bad)),
                         "business_correct": 1 if r["status"] == "pass" else 0, "expression_ok": 0, "evidence": {"claims": bad, "judge": j.get("judge")}})
            # 4. A/B differences on the same task.
            for task_id in {t for t, _ in by_task_group}:
                a = by_task_group.get((task_id, "A"), [])
                b = by_task_group.get((task_id, "B"), [])
                if a and b:
                    ma = statistics.fmean(1.0 if r["status"] == "pass" else 0.0 for r in a)
                    mb = statistics.fmean(1.0 if r["status"] == "pass" else 0.0 for r in b)
                    if ma != mb:
                        add({"kind": "dispute", "channel": "ab_diff", "task_id": task_id, "run_dir": run_dir, "group_name": None, "signature": f"{ma:.2f}|{mb:.2f}",
                             "trial_ids": [r["trial_id"] for r in a + b], "title": f"A/B differ on {task_id}: A {ma:.2f} vs B {mb:.2f}", "confidence": "n/a", "attribution": "unknown",
                             "impact": "paired difference", "recoverable": "n/a", "cluster": "ab_diff", "evidence": {"mean_a": ma, "mean_b": mb}})
            # 5. Random 10% success sample, stratified by business line x language, drawn with a seed and stored probability.
            rng = random.Random(seed)
            strata: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for r in trials:
                if r["status"] == "pass":
                    t = tasks.get(r["task_id"], {})
                    strata[(str(t.get("business_line")), str(t.get("language")))].append(r)
            for key, rows in sorted(strata.items()):
                rows = sorted(rows, key=lambda r: r["trial_id"])
                k = max(1, round(sample_rate * len(rows)))
                for r in rng.sample(rows, k):
                    add({"kind": "goodcase", "channel": "random_sample", "task_id": r["task_id"], "run_dir": run_dir, "group_name": r["group_name"], "signature": r["trial_id"],
                         "trial_ids": [r["trial_id"]], "title": f"sampled success {r['group_name']} {r['task_id']}", "confidence": "n/a", "attribution": "n/a",
                         "impact": "n/a", "recoverable": "n/a", "cluster": "random_sample", "business_correct": 1, "sampling_prob": k / len(rows),
                         "evidence": {"stratum": {"business_line": key[0], "language": key[1]}, "stratum_size": len(rows), "reads": r["reads"], "artifact": r["artifact_path"]}})
            # 6. Targeted: stable successes (all trials pass in both groups) and shorter legal paths.
            for task_id in {t for t, _ in by_task_group}:
                a = by_task_group.get((task_id, "A"), [])
                b = by_task_group.get((task_id, "B"), [])
                if a and b and all(r["status"] == "pass" for r in a + b) and len(a) >= 3 and len(b) >= 3:
                    add({"kind": "goodcase", "channel": "targeted_stable", "task_id": task_id, "run_dir": run_dir, "group_name": None, "signature": "stable",
                         "trial_ids": [r["trial_id"] for r in a + b], "title": f"stable success on {task_id} ({len(a)}+{len(b)} trials)", "confidence": "n/a", "attribution": "n/a",
                         "impact": "capability to protect", "recoverable": "n/a", "cluster": "stable", "business_correct": 1, "evidence": {"trials_a": len(a), "trials_b": len(b)}})
                if a and b:
                    ra = [r["reads"] for r in a if r["status"] == "pass" and r["reads"] is not None]
                    rb = [r["reads"] for r in b if r["status"] == "pass" and r["reads"] is not None]
                    if ra and rb and min(rb) < min(ra):
                        add({"kind": "goodcase", "channel": "targeted_alternative_path", "task_id": task_id, "run_dir": run_dir, "group_name": "B", "signature": f"reads {min(rb)}<{min(ra)}",
                             "trial_ids": [r["trial_id"] for r in b if r["status"] == "pass"], "title": f"B reached the same outcome with fewer reads on {task_id} ({min(rb)} vs {min(ra)})",
                             "confidence": "n/a", "attribution": "n/a", "impact": "legal alternative path", "recoverable": "n/a", "cluster": "alternative_path", "business_correct": 1,
                             "evidence": {"reads_a_min": min(ra), "reads_b_min": min(rb)}})
            self.conn.execute("UPDATE imports SET findings_created = findings_created + ? WHERE run_dir=?", (created, run_dir))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"run_dir": run_dir, "trials": len(trials), "findings_created": created, "findings_total": self.conn.execute("SELECT COUNT(*) FROM findings WHERE run_dir=?", (run_dir,)).fetchone()[0]}

    def add_manual(self, *, kind: str, channel: str, task_id: str, run_dir: str, title: str, by: str, trial_ids: Sequence[str] = (), group_name: str | None = None,
                   evidence: Mapping[str, Any] | None = None, hypothesis: str | None = None, attribution: str = "unknown", business_correct: int | None = None) -> dict[str, Any]:
        """A finding from outside the miner: a user correction, a historical run, a scoring dispute raised by a reviewer."""
        if kind not in KINDS:
            raise ValueError(kind)
        now = utc_now()
        signature = json.dumps({"title": title, "trials": sorted(trial_ids)}, sort_keys=True)
        fid = "f-" + _digest({"kind": kind, "channel": channel, "task_id": task_id, "run_dir": run_dir, "group_name": group_name, "signature": signature})[:16]
        if self.get(fid):
            return self.get(fid)  # type: ignore[return-value]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, kind, channel, task_id, run_dir, group_name, json.dumps(list(trial_ids)), max(1, len(trial_ids)), title, signature, None, hypothesis, "low", attribution,
                 None, None, channel, "open", business_correct, None, None, json.dumps(dict(evidence or {}), ensure_ascii=False),
                 json.dumps([{"by": by, "at": now, "action": "added", "note": channel}]), json.dumps({}), None, now, now),
            )
            self.conn.execute("INSERT INTO audit (at, actor, action, finding_id, payload_json) VALUES (?,?,?,?,?)", (now, by, "added", fid, json.dumps({"channel": channel})))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(fid)  # type: ignore[return-value]

    # -- review, promotion, links ---------------------------------------------------------------------

    def get(self, finding_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
        return _decode(row) if row else None

    def list(self, *, kind: str | None = None, status: str | None = None, run_dir: str | None = None, channel: str | None = None, task_id: str | None = None) -> list[dict[str, Any]]:
        clauses, args = [], []
        for col, val in (("kind", kind), ("status", status), ("run_dir", run_dir), ("channel", channel), ("task_id", task_id)):
            if val:
                clauses.append(f"{col}=?")
                args.append(val)
        sql = "SELECT * FROM findings" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at, finding_id"
        return [_decode(r) for r in self.conn.execute(sql, args)]

    def review(self, finding_id: str, *, by: str, action: str, note: str = "", status: str | None = None, attribution: str | None = None,
               hypothesis: str | None = None, confidence: str | None = None, expect_updated_at: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        """Append a review entry. Version conflicts and replays are refused, never silently merged."""
        row = self.get(finding_id)
        if row is None:
            raise KeyError(finding_id)
        if expect_updated_at and expect_updated_at != row["updated_at"]:
            raise RuntimeError(f"version conflict: finding updated at {row['updated_at']}, expected {expect_updated_at}")
        if status and status not in STATUSES:
            raise ValueError(f"status {status!r}")
        if attribution and attribution not in ATTRIBUTIONS:
            raise ValueError(f"attribution {attribution!r}")
        if idempotency_key and any(r.get("idempotency_key") == idempotency_key for r in row["review"]):
            return row
        now = utc_now()
        entry = {"by": by, "at": now, "action": action, "note": note, "status": status, "attribution": attribution, "idempotency_key": idempotency_key}
        history = row["review"] + [entry]
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "UPDATE findings SET review_json=?, status=COALESCE(?, status), attribution=COALESCE(?, attribution), hypothesis=COALESCE(?, hypothesis), confidence=COALESCE(?, confidence), updated_at=? WHERE finding_id=? AND updated_at=?",
                (json.dumps(history, ensure_ascii=False), status, attribution, hypothesis, confidence, now, finding_id, row["updated_at"]),
            )
            self.conn.execute("INSERT INTO audit (at, actor, action, finding_id, payload_json) VALUES (?,?,?,?,?)", (now, by, action, finding_id, json.dumps(entry, ensure_ascii=False)))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(finding_id) or row

    def link(self, finding_id: str, *, by: str, key: str, value: Any, note: str = "") -> dict[str, Any]:
        """Attach a link: regression task id, fix version, retest run, quality reference."""
        row = self.get(finding_id)
        if row is None:
            raise KeyError(finding_id)
        links = dict(row["links"])
        links.setdefault(key, [])
        if value not in links[key]:
            links[key].append(value)
        now = utc_now()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("UPDATE findings SET links_json=?, updated_at=? WHERE finding_id=?", (json.dumps(links, ensure_ascii=False), now, finding_id))
            self.conn.execute("INSERT INTO audit (at, actor, action, finding_id, payload_json) VALUES (?,?,?,?,?)", (now, by, "link", finding_id, json.dumps({key: value, "note": note}, ensure_ascii=False)))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(finding_id) or row

    def promote(self, finding_id: str, *, by: str, to: str, note: str = "") -> dict[str, Any]:
        """Mark a finding as promoted to a dataset role: regression | challenge | quality_reference."""
        if to not in ("regression", "challenge", "quality_reference"):
            raise ValueError(to)
        self.link(finding_id, by=by, key="promoted_to", value=to, note=note)
        return self.review(finding_id, by=by, action=f"promote:{to}", note=note, status="promoted")

    # -- Langfuse -------------------------------------------------------------------------------------

    def langfuse_sync(self, run_dir: str | None = None, *, client: Any = None, limit: int | None = None) -> dict[str, Any]:
        """Verify each trial's trace ids can be read back; only a successful read-back marks ``verified``."""
        rows = [dict(r) for r in self.conn.execute("SELECT trial_id, trace_ids_json, langfuse_status FROM eval_trials WHERE (langfuse_status = 'pending' OR langfuse_status LIKE 'failed%')" + (" AND run_dir=?" if run_dir else ""), (run_dir,) if run_dir else ())]
        if limit:
            rows = rows[:limit]
        if client is None:
            try:
                from ..observability.tracing import Tracer

                tracer = Tracer()
                client = tracer._client if tracer.enabled else None
            except Exception:
                client = None
        counts = {"verified": 0, "failed": 0, "pending": 0, "no_client": 0}
        for r in rows:
            ids = json.loads(r["trace_ids_json"] or "[]")
            if client is None:
                counts["no_client"] += 1
                continue
            ok, why = True, ""
            for tid in ids:
                try:
                    data = client.api.observations.get_many(trace_id=tid, limit=5, request_options={"timeout_in_seconds": 20, "max_retries": 1}).data
                    if not data:
                        ok, why = False, "empty"
                except Exception as exc:  # noqa: BLE001 - recorded on the trial, retried on the next sync
                    ok, why = False, type(exc).__name__
                time.sleep(0.15)  # be polite to the API; a failed read-back is retryable, a false "verified" is not
            status = "verified" if ok and ids else f"failed:{why or 'unknown'}"
            counts["verified" if ok and ids else "failed"] += 1
            self.conn.execute("UPDATE eval_trials SET langfuse_status=? WHERE trial_id=?", (status, r["trial_id"]))
        return {"checked": len(rows), **counts}

    # -- export ------------------------------------------------------------------------------------------

    def export(self, out_dir: Path) -> dict[str, Any]:
        """Static JSON + HTML: findings, review queue, regression status, good/bad pairs, version compare."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        findings = self.list()
        trials = [dict(r) for r in self.conn.execute("SELECT trial_id, task_id, task_sha256, run_dir, group_name, trial_index, status, failure_class, final_state, conclusion_type, cost_usd, latency_s, reads, artifact_path, langfuse_status FROM eval_trials")]
        pairs = []
        by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for f in findings:
            by_task[f["task_id"]].append(f)
        for task_id, fs in by_task.items():
            good = [f for f in fs if f["kind"] == "goodcase"]
            bad = [f for f in fs if f["kind"] == "badcase"]
            if good and bad:
                pairs.append({"task_id": task_id, "good": [g["finding_id"] for g in good], "bad": [b["finding_id"] for b in bad]})
        versions: dict[str, dict[str, Any]] = defaultdict(dict)
        for t in trials:
            versions[t["task_id"]].setdefault(t["run_dir"], {"pass": 0, "fail": 0, "unknown": 0})[t["status"]] += 1
        regression = [f for f in findings if "regression" in (f["links"].get("promoted_to") or [])]
        data = {
            "exported_at": utc_now(), "findings": findings, "trials": trials, "pairs": pairs, "version_compare": versions,
            "review_queue": [f for f in findings if f["status"] in ("open", "reviewing")], "regression_links": regression,
            "audit": [dict(r) for r in self.conn.execute("SELECT * FROM audit ORDER BY seq")],
        }
        (out_dir / "findings.json").write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        (out_dir / "index.html").write_text(_html(data), encoding="utf-8")
        return {"findings": len(findings), "trials": len(trials), "pairs": len(pairs), "review_queue": len(data["review_queue"]), "out": str(out_dir)}


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("trial_ids", "review", "links", "evidence", "versions"):
        d[key] = json.loads(d.pop(f"{key}_json") or ("[]" if key in ("trial_ids", "review") else "{}"))
    return d


def _trace_ids(run_dir: Path, trial: Mapping[str, Any]) -> list[str]:
    rel = (trial.get("artifact") or {}).get("path")
    if not rel:
        return []
    path = run_dir / rel
    if not path.exists():
        return []
    try:
        art = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return sorted({e["payload"]["trace_id"] for e in art.get("events", []) if e.get("kind") == "observation_started" and (e.get("payload") or {}).get("trace_id")})


def _first_deviation(run_dir: str, trial: Mapping[str, Any]) -> str | None:
    """Earliest observable divergence: the first failed check, in the layer order outcome -> behaviour."""
    layers = json.loads(trial.get("layers_json") or "{}")
    for layer in ("behaviour", "outcome"):
        for k, v in (layers.get(layer, {}).get("checks") or {}).items():
            if v is False:
                return f"{layer}.{k}"
    return trial.get("failure_class")


def _html(data: Mapping[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>Case findings</title>
<style>body{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#222}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;vertical-align:top}
.k-badcase{color:#a40000}.k-goodcase{color:#0a6d1e}.k-dispute{color:#8a5a00}code{font-size:12px}.muted{color:#666}input,select{margin-right:8px}</style></head><body>
<h1>案例台（静态导出）</h1><p class="muted" id="meta"></p>
<p><label>kind <select id="kind"><option value="">all</option><option>badcase</option><option>goodcase</option><option>dispute</option></select></label>
<label>status <select id="status"><option value="">all</option><option>open</option><option>reviewing</option><option>confirmed</option><option>fixed</option><option>wontfix</option><option>promoted</option><option>not_a_defect</option></select></label>
<label>channel <input id="channel" placeholder="auto_failure / safety / response / ab_diff / random_sample / targeted_*"></label>
<label>task <input id="task" placeholder="task id substring"></label></p>
<h2>发现 <span id="count"></span></h2><table id="t"><thead><tr><th>id</th><th>kind</th><th>channel</th><th>task</th><th>group</th><th>title</th><th>first deviation</th><th>attribution</th><th>status</th><th>n</th><th>p</th><th>links</th><th>review</th></tr></thead><tbody></tbody></table>
<h2>good / bad 配对</h2><div id="pairs"></div>
<h2>版本对比（按任务 × 运行）</h2><div id="versions"></div>
<h2>审计</h2><div id="audit" class="muted"></div>
<script>const D=""" + payload + """;
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
document.getElementById('meta').textContent='exported '+D.exported_at+' · '+D.findings.length+' findings · '+D.trials.length+' trials · review queue '+D.review_queue.length;
function render(){const k=kind.value,s=status.value,c=channel.value,t=task.value;const rows=D.findings.filter(f=>(!k||f.kind===k)&&(!s||f.status===s)&&(!c||f.channel.includes(c))&&(!t||f.task_id.includes(t)));
document.getElementById('count').textContent='('+rows.length+')';
document.querySelector('#t tbody').innerHTML=rows.map(f=>`<tr><td><code>${esc(f.finding_id)}</code></td><td class="k-${f.kind}">${f.kind}</td><td>${esc(f.channel)}</td><td><code>${esc(f.task_id)}</code></td><td>${esc(f.group_name)}</td><td>${esc(f.title)}</td><td>${esc(f.first_deviation)}</td><td>${esc(f.attribution)}</td><td>${esc(f.status)}</td><td>${f.occurrences}</td><td>${f.sampling_prob==null?'':f.sampling_prob.toFixed(2)}</td><td>${esc(JSON.stringify(f.links))}</td><td>${f.review.map(r=>esc(r.at.slice(0,16)+' '+r.by+': '+r.action+(r.note?' — '+r.note:''))).join('<br>')}</td></tr>`).join('');}
for(const id of ['kind','status','channel','task'])document.getElementById(id).addEventListener('input',render);render();
document.getElementById('pairs').innerHTML=D.pairs.length?'<ul>'+D.pairs.map(p=>`<li><code>${esc(p.task_id)}</code>: good ${p.good.map(esc).join(', ')} / bad ${p.bad.map(esc).join(', ')}</li>`).join('')+'</ul>':'<p class="muted">none</p>';
document.getElementById('versions').innerHTML='<table><tr><th>task</th><th>run</th><th>pass</th><th>fail</th><th>unknown</th></tr>'+Object.entries(D.version_compare).flatMap(([t,runs])=>Object.entries(runs).map(([r,c])=>`<tr><td><code>${esc(t)}</code></td><td>${esc(r)}</td><td>${c.pass}</td><td>${c.fail}</td><td>${c.unknown}</td></tr>`)).join('')+'</table>';
document.getElementById('audit').innerHTML=D.audit.slice(-200).map(a=>esc(a.at.slice(0,19)+' '+a.actor+' '+a.action+' '+(a.finding_id||''))).join('<br>');
</script></body></html>"""
