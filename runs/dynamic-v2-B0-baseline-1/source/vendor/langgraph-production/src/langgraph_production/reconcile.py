"""The human end of :class:`~langgraph_production.effects.EffectInDoubt`.

    python -m langgraph_production.reconcile --db state.sqlite3
    python -m langgraph_production.reconcile --db state.sqlite3 --key demo-TCK-023:1270c45d1612d5d1 \
        --outcome not_performed --resolver ops.dana
    python -m langgraph_production.reconcile --db state.sqlite3 --key ... \
        --outcome performed --resolver ops.dana --result '{"refunded_cents": 4500}'

A graph that stops and refuses to guess is only half of the story. Without a
way to answer it, the ticket stops for good, which is a different failure and
not a better one. With no ``--key`` this lists the open claims and exits 0.
Exit code 1 when a resolution is refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .effects import (
    ReconciliationError,
    SqliteEffectStore,
    open_effects,
    resolve_effect,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m langgraph_production.reconcile",
        description="List and close effect claims that a run left open.",
    )
    parser.add_argument("--db", required=True, type=Path, help="the SQLite file the runtime uses")
    parser.add_argument("--key", help="idempotency key to resolve, as named in the error")
    parser.add_argument(
        "--outcome",
        choices=["performed", "not_performed"],
        help="what you found downstream: the work was done, or it was not",
    )
    parser.add_argument("--resolver", default="", help="who looked, recorded on the row")
    parser.add_argument(
        "--result",
        default=None,
        help="JSON the downstream system reports, stored as the receipt result",
    )
    return parser


def _print_open(store: SqliteEffectStore) -> None:
    rows = open_effects(store)
    if not rows:
        print("no open effect claims")
        return
    print(f"open effect claims: {len(rows)}")
    for key, row in sorted(rows.items()):
        executions = store.execution_count(key)
        print(f"  {key}")
        print(f"    tool        {row.get('tool')}")
        print(f"    status      {row.get('status')}")
        print(f"    claimed at  {row.get('claimed_at')}")
        print(f"    executions  {executions} recorded on disk")
        if row.get("error"):
            print(f"    error       {row['error']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.db.exists():
        print(f"no such database: {args.db}", file=sys.stderr)
        return 1

    store = SqliteEffectStore(args.db)
    try:
        if args.key is None:
            _print_open(store)
            return 0

        if args.outcome is None:
            print("--outcome is required when --key is given", file=sys.stderr)
            return 1
        if not args.resolver:
            print("--resolver is required: a reconciliation is signed", file=sys.stderr)
            return 1

        result = json.loads(args.result) if args.result else None
        try:
            row = resolve_effect(
                store=store,
                key=args.key,
                outcome=args.outcome,
                resolver=args.resolver,
                result=result,
            )
        except ReconciliationError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        print(f"{args.key} resolved as {args.outcome} by {args.resolver}")
        print(json.dumps(row, indent=2, sort_keys=True))
        if args.outcome == "not_performed":
            print("the claim is released: the next run of this ticket will call the handler")
        else:
            print("the claim is closed: the next run of this ticket replays this receipt")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
