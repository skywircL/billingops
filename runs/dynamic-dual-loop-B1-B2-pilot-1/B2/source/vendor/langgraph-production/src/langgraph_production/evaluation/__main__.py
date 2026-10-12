"""CLI for the evaluation harness. This is what CI runs.

    python -m langgraph_production.evaluation
    python -m langgraph_production.evaluation --json runs/report.json
    python -m langgraph_production.evaluation --skip-resume

Exit code 0 when every threshold holds, 1 when one does not.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .report import load_thresholds, run_evaluation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m langgraph_production.evaluation",
        description="Measure routing, interrupt correctness, and resume determinism.",
    )
    parser.add_argument("--fixtures", type=Path, default=None, help="path to a JSONL fixture file")
    parser.add_argument("--thresholds", type=Path, default=None, help="path to a thresholds JSON file")
    parser.add_argument("--json", type=Path, default=None, help="also write the report as JSON")
    parser.add_argument(
        "--resume-limit",
        type=int,
        default=16,
        help="how many tickets to sweep crash points over (default 16)",
    )
    parser.add_argument(
        "--skip-resume",
        action="store_true",
        help="skip the crash sweep, which is the slow part",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.5,
        help="router confidence below which a decision is not acted on",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_evaluation(
        fixtures_path=args.fixtures,
        thresholds=load_thresholds(args.thresholds),
        min_confidence=args.min_confidence,
        resume_limit=args.resume_limit,
        skip_resume=args.skip_resume,
    )
    print(report.render())

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
        print(f"\nreport written to {args.json}")

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
