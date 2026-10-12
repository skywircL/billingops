"""``python -m billingops.evaluation tasks …``: dataset validation, reference execution, freezing.

    tasks validate data/tasks/sealed-v1.json --sealed          # schema + relations + lineage isolation
    tasks reference data/tasks/sealed-v1.json --out runs/ref   # PathModel reference execution, $0
    tasks summary data/tasks/sealed-v1.json                    # counts only: what the dev side may see
    tasks freeze data/tasks/sealed-v1.json --role "..."        # manifest with per-task digests
    tasks mutate --out data/tasks/coverage-v1.candidates.json   # controlled mutation of the seed sets ($0, no model)
    tasks accept --candidates <file> --out <file> --report <dir> # four acceptance checks + human-review sample
    tasks adapt-legacy                                         # read-only adapter for the pre-contract sets
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from ..billing_client import BillingClient
from ..testing import BillingServer
from .artifacts import write_json
from .data import DATA_DIR, load_set
from .tasks import (
    EvalTask,
    family_summary,
    freeze,
    from_legacy_case,
    lineage_violations,
    load_tasks,
    occupied_from_matrix,
    utc_now,
    validate_task,
    write_tasks,
)

DOCS_DIR = DATA_DIR.parent / "docs"
TASKS_DIR = DATA_DIR / "tasks"


def cmd_validate(path: Path, *, sealed: bool, matrix: Path) -> dict[str, Any]:
    tasks = load_tasks(path)
    errors = {t.task_id: validate_task(t) for t in tasks}
    errors = {k: v for k, v in errors.items() if v}
    ids = [t.task_id for t in tasks]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    lineage = lineage_violations(tasks, occupied_from_matrix(matrix)) if sealed else []
    per_family: dict[str, list[EvalTask]] = {}
    for t in tasks:
        per_family.setdefault(t.family, []).append(t)
    family_problems = []
    if sealed:
        for fam, members in per_family.items():
            langs = sorted(t.language for t in members)
            if len(members) != 4:
                family_problems.append(f"{fam}: {len(members)} variants, expected 4")
            if langs != ["en", "mixed", "zh", "zh"]:
                family_problems.append(f"{fam}: languages {langs}, expected 2 zh / 1 en / 1 mixed")
            mechs = {tuple(t.mechanisms) for t in members}
            if len(mechs) != 1:
                family_problems.append(f"{fam}: variants disagree on mechanisms {sorted(mechs)}")
            non_language_axes = {(t.disclosure, json.dumps(t.fault_plan, sort_keys=True), json.dumps(t.approval_script, sort_keys=True), t.user_text) for t in members}
            if len(non_language_axes) < 2:
                family_problems.append(f"{fam}: variants differ only in language")
        lines = {b: sum(1 for t in tasks if t.business_line == b) for b in ("duplicate", "refund_status", "subscription")}
        fams = {b: len({t.family for t in tasks if t.business_line == b}) for b in lines}
        if fams != {"duplicate": 6, "refund_status": 3, "subscription": 3}:
            family_problems.append(f"family allocation {fams}, expected duplicate 6 / refund_status 3 / subscription 3")
    return {
        "path": str(path), "tasks": len(tasks), "valid": not errors and not dupes and not lineage and not family_problems,
        "errors": errors, "duplicate_ids": dupes, "lineage_violations": lineage, "family_problems": family_problems,
        "summary": family_summary(tasks),
    }


def cmd_reference(path: Path, *, out: Path, summary_only: bool) -> dict[str, Any]:
    from .trials import run_trial

    tasks = load_tasks(path)
    if out.exists():
        raise FileExistsError(f"{out} exists; reference runs are never overwritten")
    out.mkdir(parents=True)
    tmp = tempfile.mkdtemp(prefix="billingops-reference-")
    server = BillingServer(Path(tmp) / "billing.sqlite3").start()
    rows: list[dict[str, Any]] = []
    try:
        billing = BillingClient(server.url, admin_token="demo-admin")
        for task in tasks:
            r = run_trial(task, "dynamic", model_spec="path", billing=billing, workdir=out / "dbs")
            rows.append({
                "task_id": task.task_id, "family": task.family, "status": r.status, "failure_class": r.failure_class,
                "final_state": r.final_state, "conclusion_type": r.conclusion_type,
                "outcome": r.layers["outcome"].get("checks"), "behaviour": r.layers["behaviour"].get("checks"),
                "forbidden_changes": r.changes.get("forbidden", []), "safety": r.safety, "error": r.error,
            })
        billing.close()
    finally:
        server.stop()
    with (out / "reference.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {
        "path": str(path), "ran_at": utc_now(), "tasks": len(rows),
        "passed": sum(1 for r in rows if r["status"] == "pass"),
        "failed": sum(1 for r in rows if r["status"] == "fail"),
        "unknown": sum(1 for r in rows if r["status"] == "unknown"),
        "note": "PathModel reference execution: proves the environment serves the task's facts and the expected outcome follows; says nothing about any agent.",
    }
    write_json(out / "summary.json", summary)
    return summary if summary_only else {**summary, "rows": rows}


def cmd_adapt_legacy() -> dict[str, Any]:
    """Read-only adapter for pre-contract sets. Exposure is recorded per set, never recomputed."""
    sources = [
        ("dev", DATA_DIR / "dev_cases.json", "legacy-dev", "exposed"),
        ("heldout-v2", DATA_DIR / "heldout_cases.json", "legacy-heldout-v2", "exposed"),
        ("challenge-v1", DATA_DIR / "challenge_cases_v1.json", "legacy-challenge-v1", "exposed"),
        ("v3-validation", DATA_DIR / "v3_validation.json", "legacy-v3-validation", "exposed"),
        ("v3-legacy-regression", DATA_DIR / "v3_legacy_regression.json", "legacy-v3-regression", "exposed"),
    ]
    written: dict[str, Any] = {}
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    for name, src, dataset, exposure in sources:
        if not src.exists():
            continue
        raw = json.loads(src.read_text(encoding="utf-8"))
        tasks = [from_legacy_case(c, dataset=dataset, exposure=exposure) for c in raw]
        for t in tasks:
            t.exposure["exposed_by"] = "pre-contract runs; see data/assets_manifest.json"
        target = TASKS_DIR / f"{dataset}.json"
        write_tasks(target, tasks)
        written[name] = {"tasks": len(tasks), "path": str(target), "invalid": {t.task_id: e for t in tasks if (e := validate_task(t))}}
    return written


def add_parser(sub: Any) -> None:
    p = sub.add_parser("tasks")
    s = p.add_subparsers(dest="tasks_command", required=True)
    v = s.add_parser("validate")
    v.add_argument("path", type=Path)
    v.add_argument("--sealed", action="store_true", help="also enforce sealed-set structure and lineage isolation")
    v.add_argument("--matrix", type=Path, default=DOCS_DIR / "CAPABILITY_MATRIX.md")
    r = s.add_parser("reference")
    r.add_argument("path", type=Path)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--summary-only", action="store_true")
    m = s.add_parser("summary")
    m.add_argument("path", type=Path)
    f = s.add_parser("freeze")
    f.add_argument("path", type=Path)
    f.add_argument("--role", required=True)
    s.add_parser("adapt-legacy")
    mu = s.add_parser("mutate")
    mu.add_argument("--rewrites", type=Path, default=DATA_DIR / "mutations" / "rewrites-v1.json")
    mu.add_argument("--out", type=Path, default=TASKS_DIR / "coverage-v1.candidates.json")
    ac = s.add_parser("accept")
    ac.add_argument("--candidates", type=Path, default=TASKS_DIR / "coverage-v1.candidates.json")
    ac.add_argument("--out", type=Path, default=TASKS_DIR / "coverage-v1.json")
    ac.add_argument("--report", type=Path, default=DATA_DIR.parent / "analysis" / "coverage-v1")
    ac.add_argument("--no-reference", action="store_true", help="skip the reference execution (rules and dedup only)")
    b = s.add_parser("bank")
    b.add_argument("which", choices=["gold", "canary", "canary-v2", "canary-v3", "regression", "regression-v1.1", "fixes", "fixes-v2"])
    j = sub.add_parser("judge")
    js = j.add_subparsers(dest="judge_command", required=True)
    jr = js.add_parser("run")
    jr.add_argument("--run", type=Path, required=True)
    jr.add_argument("--tasks", type=Path, required=True)
    jr.add_argument("--out", type=Path, default=None)
    jr.add_argument("--limit", type=int, default=None)
    jr.add_argument("--trial-index", type=int, default=None)
    ja = js.add_parser("annotation-set")
    ja.add_argument("--run", type=Path, required=True)
    ja.add_argument("--tasks", type=Path, required=True)
    ja.add_argument("--historical", type=Path, nargs="*", default=[])
    ja.add_argument("--out", type=Path, required=True)
    ja.add_argument("--historical-n", type=int, default=16)
    jf = js.add_parser("refresh-deterministic")
    jf.add_argument("--run", type=Path, required=True)
    jf.add_argument("--tasks", type=Path, required=True)
    ji = js.add_parser("ingest-labels")
    ji.add_argument("--sheet", type=Path, required=True)
    ji.add_argument("--labels", type=Path, required=True)
    ji.add_argument("--by", required=True)
    ji.add_argument("--provenance", required=True, help="e.g. 'assisted: model pre-annotation reviewed by the user' or 'independent human'")
    ji.add_argument("--out", type=Path, default=None)
    jj = js.add_parser("rejudge-items")
    jj.add_argument("--folder", type=Path, required=True)
    jj.add_argument("--tasks", type=Path, required=True)
    jc = js.add_parser("calibrate")
    jc.add_argument("--judged", type=Path, required=True)
    jc.add_argument("--labels", type=Path, required=True)
    c = sub.add_parser("cases")
    cs = c.add_subparsers(dest="cases_command", required=True)
    for name in ("import", "mine"):
        x = cs.add_parser(name)
        x.add_argument("--run", type=Path, required=True)
        x.add_argument("--db", type=Path, default=Path("data/cases.sqlite3"))
        if name == "mine":
            x.add_argument("--seed", type=int, default=20260921)
        else:
            x.add_argument("--results-name", default=None, help="import <run>/<name>/results.jsonl (a rescore) instead of the as-run results")
    ca = cs.add_parser("add")
    ca.add_argument("--db", type=Path, default=Path("data/cases.sqlite3"))
    ca.add_argument("--kind", required=True, choices=["badcase", "goodcase", "dispute"])
    ca.add_argument("--channel", required=True)
    ca.add_argument("--task", required=True)
    ca.add_argument("--run", required=True)
    ca.add_argument("--title", required=True)
    ca.add_argument("--by", required=True)
    ca.add_argument("--trial", action="append", default=[])
    ca.add_argument("--group", default=None)
    ca.add_argument("--evidence", default="{}")
    ca.add_argument("--hypothesis", default=None)
    ca.add_argument("--attribution", default="unknown")
    cl = cs.add_parser("list")
    cl.add_argument("--db", type=Path, default=Path("data/cases.sqlite3"))
    for opt in ("kind", "status", "run", "channel", "task"):
        cl.add_argument(f"--{opt}", default=None)
    cr = cs.add_parser("review")
    cr.add_argument("finding_id")
    cr.add_argument("--db", type=Path, default=Path("data/cases.sqlite3"))
    cr.add_argument("--by", required=True)
    cr.add_argument("--action", required=True)
    cr.add_argument("--note", default="")
    cr.add_argument("--status", default=None)
    cr.add_argument("--attribution", default=None)
    cr.add_argument("--hypothesis", default=None)
    cr.add_argument("--confidence", default=None)
    cr.add_argument("--expect-updated-at", default=None)
    cr.add_argument("--idempotency-key", default=None)
    cp = cs.add_parser("promote")
    cp.add_argument("finding_id")
    cp.add_argument("--db", type=Path, default=Path("data/cases.sqlite3"))
    cp.add_argument("--by", required=True)
    cp.add_argument("--to", required=True, choices=["regression", "challenge", "quality_reference"])
    cp.add_argument("--note", default="")
    ck = cs.add_parser("link")
    ck.add_argument("finding_id")
    ck.add_argument("--db", type=Path, default=Path("data/cases.sqlite3"))
    ck.add_argument("--by", required=True)
    ck.add_argument("--key", required=True)
    ck.add_argument("--value", required=True)
    ck.add_argument("--note", default="")
    ce = cs.add_parser("export")
    ce.add_argument("--db", type=Path, default=Path("data/cases.sqlite3"))
    ce.add_argument("--out", type=Path, required=True)
    cf = cs.add_parser("langfuse-sync")
    cf.add_argument("--db", type=Path, default=Path("data/cases.sqlite3"))
    cf.add_argument("--run", default=None)
    cf.add_argument("--limit", type=int, default=None)
    pr = sub.add_parser("prefix-run")
    pr.add_argument("--artifact", type=Path, required=True)
    pr.add_argument("--switch-at", type=int, required=True)
    pr.add_argument("--model", default="openai")
    pr.add_argument("--out", type=Path, required=True)
    rc = sub.add_parser("release-check")
    rc.add_argument("--sealed-run", type=Path, required=True)
    rc.add_argument("--group", default="A")
    rc.add_argument("--threshold", type=float, default=0.95)
    rc.add_argument("--slice-threshold", type=float, default=0.90)
    rc.add_argument("--regression-run", type=Path, default=None)
    rc.add_argument("--regression-must-pass", default=None, help="comma-separated task ids; others in the regression run are reported, not gated")
    rc.add_argument("--out", type=Path, default=None)
    rc.add_argument("--results-name", default=None, help="use <run>/<name>/results.jsonl (a rescore) instead of the as-run results")
    rc.add_argument("--judge-name", default="judge", help="which judge pass to read: <run>/<name>/judge.jsonl")
    q = sub.add_parser("suite")
    qs = q.add_subparsers(dest="suite_command", required=True)
    sr = qs.add_parser("run")
    sr.add_argument("--tasks", type=Path, required=True)
    sr.add_argument("--out", type=Path, required=True)
    sr.add_argument("--groups", default="A,B")
    sr.add_argument("--trials", type=int, default=3)
    sr.add_argument("--key-risk-trials", type=int, default=7)
    sr.add_argument("--model", default="fake", help="fake | openai | path")
    sr.add_argument("--workers", type=int, default=1)
    sr.add_argument("--budget-usd", type=float, default=None)
    sr.add_argument("--seed", type=int, default=20260921)
    sr.add_argument("--label", default=None)
    sr.add_argument("--limit", type=int, default=None)
    sr.add_argument("--task-ids", default=None, help="comma-separated subset of task ids")
    sc = qs.add_parser("compare")
    sc.add_argument("--before", type=Path, required=True)
    sc.add_argument("--after", type=Path, required=True)
    sc.add_argument("--group", default="A")
    sc.add_argument("--results-name", default=None)
    srs = qs.add_parser("rescore")
    srs.add_argument("--run", type=Path, required=True)
    srs.add_argument("--tasks", type=Path, default=None, help="revised expectations; defaults to the run's own tasks.json")
    srs.add_argument("--out-name", default=None)
    sro = qs.add_parser("reclassify-outage", help="mark trials touched by a provider outage as infra_model/unknown (runs before structured model failures)")
    sro.add_argument("--run", type=Path, required=True)
    sro.add_argument("--since", required=True, help="ISO timestamp of the first refused call")
    sro.add_argument("--evidence", required=True, help="how the provider was established as the cause, e.g. a probe result")
    sro.add_argument("--out-name", default="reclass-infra-1")


def run_judge_cli(args: Any) -> Any:
    from .judge import calibrate, judge_run

    if args.judge_command == "calibrate":
        return calibrate(args.judged, args.labels)
    if args.judge_command == "ingest-labels":
        from .annotation import ingest_sheet

        return ingest_sheet(args.sheet, args.labels, annotated_by=args.by, provenance=args.provenance, out=args.out)
    if args.judge_command == "rejudge-items":
        from ..llm.openai_compat import OpenAICompatibleModel as _Model
        from .annotation import rejudge_items

        return rejudge_items(args.folder, calibration_tasks=args.tasks, model=_Model())
    if args.judge_command == "refresh-deterministic":
        from .judge import refresh_deterministic

        return refresh_deterministic(args.run, load_tasks(args.tasks))
    from ..llm.openai_compat import OpenAICompatibleModel

    if args.judge_command == "annotation-set":
        from .annotation import build_annotation_set

        return build_annotation_set(calibration_run=args.run, calibration_tasks=args.tasks, historical_runs=list(args.historical), out=args.out, model=OpenAICompatibleModel(), historical_n=args.historical_n)

    return judge_run(args.run, load_tasks(args.tasks), model=OpenAICompatibleModel(), out=args.out, limit=args.limit, trial_index=args.trial_index)


def run_cases_cli(args: Any) -> Any:
    from .findings import FindingsDB

    db = FindingsDB(args.db)
    try:
        cmd = args.cases_command
        if cmd == "import":
            return db.import_run(args.run, results_name=args.results_name)
        if cmd == "mine":
            return db.mine(args.run, seed=args.seed)
        if cmd == "add":
            return db.add_manual(kind=args.kind, channel=args.channel, task_id=args.task, run_dir=args.run, title=args.title, by=args.by, trial_ids=args.trial, group_name=args.group, evidence=json.loads(args.evidence), hypothesis=args.hypothesis, attribution=args.attribution)
        if cmd == "list":
            return db.list(kind=args.kind, status=args.status, run_dir=args.run, channel=args.channel, task_id=args.task)
        if cmd == "review":
            return db.review(args.finding_id, by=args.by, action=args.action, note=args.note, status=args.status, attribution=args.attribution, hypothesis=args.hypothesis, confidence=args.confidence, expect_updated_at=args.expect_updated_at, idempotency_key=args.idempotency_key)
        if cmd == "promote":
            return db.promote(args.finding_id, by=args.by, to=args.to, note=args.note)
        if cmd == "link":
            return db.link(args.finding_id, by=args.by, key=args.key, value=args.value, note=args.note)
        if cmd == "export":
            return db.export(args.out)
        if cmd == "langfuse-sync":
            return db.langfuse_sync(args.run, limit=args.limit)
        raise SystemExit(cmd)
    finally:
        db.close()


def run_prefix_cli(args: Any) -> Any:
    from .prefix import prefix_run

    tmp = tempfile.mkdtemp(prefix="billingops-prefix-")
    server = BillingServer(Path(tmp) / "billing.sqlite3").start()
    try:
        billing = BillingClient(server.url, admin_token="demo-admin")
        record = prefix_run(artifact=args.artifact, switch_at=args.switch_at, model_spec=args.model, out_dir=args.out, billing=billing)
        billing.close()
    finally:
        server.stop()
    return {k: v for k, v in record.items() if k != "suffix"} | {"suffix": {k: v for k, v in (record.get("suffix") or {}).items() if k != "layers"}}


def run_release_cli(args: Any) -> Any:
    from .release import release_check

    must = [t.strip() for t in args.regression_must_pass.split(",") if t.strip()] if args.regression_must_pass else None
    report = release_check(sealed_run=args.sealed_run, gate_group=args.group, threshold=args.threshold, slice_threshold=args.slice_threshold, regression_run=args.regression_run, regression_must_pass=must, results_name=args.results_name, judge_name=args.judge_name)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def run_suite_cli(args: Any) -> Any:
    from .suite import run_suite

    if args.suite_command == "compare":
        from .release import compare_runs

        return compare_runs(args.before, args.after, group=args.group, results_name=args.results_name)
    if args.suite_command == "rescore":
        from .trials import rescore_run

        return rescore_run(args.run, load_tasks(args.tasks) if args.tasks else None, out_name=args.out_name)
    if args.suite_command == "reclassify-outage":
        from .trials import reclassify_provider_outage

        return reclassify_provider_outage(args.run, since=args.since, evidence=args.evidence, out_name=args.out_name)

    return run_suite(
        tasks_path=args.tasks, out_dir=args.out, groups=tuple(g.strip() for g in args.groups.split(",") if g.strip()),
        trials=args.trials, key_risk_trials=args.key_risk_trials, model_spec=args.model, workers=args.workers,
        budget_usd=args.budget_usd, seed=args.seed, label=args.label, limit=args.limit,
        task_ids=[t.strip() for t in args.task_ids.split(",") if t.strip()] if args.task_ids else None,
    )


def run(args: Any) -> Any:
    if args.tasks_command == "validate":
        return cmd_validate(args.path, sealed=args.sealed, matrix=args.matrix)
    if args.tasks_command == "reference":
        return cmd_reference(args.path, out=args.out, summary_only=args.summary_only)
    if args.tasks_command == "summary":
        return family_summary(load_tasks(args.path))
    if args.tasks_command == "freeze":
        return freeze(args.path, role=args.role)
    if args.tasks_command == "adapt-legacy":
        return cmd_adapt_legacy()
    if args.tasks_command == "mutate":
        from .mutation import write_candidates

        return write_candidates(TASKS_DIR, args.rewrites, args.out)
    if args.tasks_command == "accept":
        from .acceptance import accept

        return accept(args.candidates, TASKS_DIR, args.out, args.report, run_reference=not args.no_reference)
    if args.tasks_command == "bank":
        from .bank import write_canary, write_canary_v2, write_canary_v3, write_fixes, write_fixes_v2, write_gold, write_regression, write_regression_v1_1

        return {"gold": write_gold, "canary": write_canary, "canary-v2": write_canary_v2, "canary-v3": write_canary_v3, "regression": write_regression,
                "regression-v1.1": write_regression_v1_1, "fixes": write_fixes, "fixes-v2": write_fixes_v2}[args.which](TASKS_DIR)
    raise SystemExit(f"unknown tasks command {args.tasks_command}")
