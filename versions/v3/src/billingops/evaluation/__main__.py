"""Evaluation commands.

    python -m billingops.evaluation data                 # write data/dev_cases.json and data/heldout_cases.json
    python -m billingops.evaluation run --set dev --model fake --groups A,B
    python -m billingops.evaluation run --set heldout --model openai --groups A,B      # the formal experiment
    python -m billingops.evaluation report runs/<dir>
    python -m billingops.evaluation record --trace data/replays/<name>.json --text "..." [--answer "..."]
"""

from __future__ import annotations

import argparse
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..billing_client import BillingClient
from ..testing import BillingServer
from .data import DATA_DIR, load_set, write_sets
from .experiment import run_experiment
from .report import write_report


def main() -> None:
    parser = argparse.ArgumentParser(prog="billingops-eval")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("data")
    run = sub.add_parser("run")
    run.add_argument("--set", default="dev", choices=["dev", "heldout"])
    run.add_argument("--model", default=os.environ.get("BILLINGOPS_MODEL", "fake"), help="fake | openai (the pinned DeepSeek model)")
    run.add_argument("--groups", default="A,B")
    run.add_argument("--limit", type=int, default=None)
    run.add_argument("--cases-file", type=Path, help="independent task set; does not overwrite dev/heldout")
    run.add_argument("--out", default=None)
    run.add_argument("--label", default=None)
    rep = sub.add_parser("report")
    rep.add_argument("dir")
    for name in ("review-export", "rescore"):
        analysis = sub.add_parser(name)
        analysis.add_argument("dir", type=Path)
        analysis.add_argument("--tasks", type=Path, default=DATA_DIR / "heldout_cases.json")
        analysis.add_argument("--out", type=Path)
    audit = sub.add_parser("trace-check")
    audit.add_argument("--out", type=Path, required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--trace", required=True)
    rec.add_argument("--text", required=True)
    rec.add_argument("--answer", default=None)
    rec.add_argument("--mode", default="dynamic")
    rec.add_argument("--model", default=os.environ.get("BILLINGOPS_MODEL", "openai"))
    rec.add_argument("--world", default="duplicate", choices=["duplicate", "duplicate_partial", "ambiguous"])
    args = parser.parse_args()

    if args.command == "review-export":
        from .review import export_review
        print(export_review(args.dir, tasks_path=args.tasks, output=args.out))
        return
    if args.command == "rescore":
        from .rescore import rescore_run
        print(rescore_run(args.dir, tasks_path=args.tasks, output=args.out))
        return
    if args.command == "trace-check":
        from ..observability.acceptance import run_acceptance
        print(run_acceptance(args.out))
        return

    if args.command == "data":
        print(write_sets(DATA_DIR))
        return
    if args.command == "report":
        print(write_report(Path(args.dir)))
        return
    if args.command == "record":
        from .recording import record_trajectory

        print(record_trajectory(trace_path=Path(args.trace), text=args.text, answer=args.answer, mode=args.mode, model_spec=args.model, world=args.world))
        return

    if args.cases_file:
        import json
        from .data import Case
        cases = [Case(**item) for item in json.loads(args.cases_file.read_text())]
    else:
        cases = load_set(args.set)
    if args.limit:
        cases = cases[: args.limit]
    groups = tuple(g.strip() for g in args.groups.split(",") if g.strip())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = args.label or f"{args.cases_file.stem if args.cases_file else args.set}-{args.model}"
    out_dir = Path(args.out) if args.out else Path("runs") / f"{stamp}-{label}"
    tmp = tempfile.mkdtemp(prefix="billingops-eval-billing-")
    server = BillingServer(Path(tmp) / "billing.sqlite3").start()
    try:
        billing = BillingClient(server.url, admin_token="demo-admin")
        run_experiment(cases, groups=groups, model_spec=args.model, billing=billing, out_dir=out_dir, label=label)
        billing.close()
    finally:
        server.stop()
    print(write_report(out_dir))


if __name__ == "__main__":
    main()
