"""Run one ticket through the graph and show every step.

    python -m langgraph_production --text "Please refund $45.00 from invoice INV-10032"
    python -m langgraph_production --fixture TCK-023 --deny
    python -m langgraph_production --fixture TCK-023 --crash-at act:after_effect

Everything runs against the offline stand in, so this needs no key and no
network. ``--crash-at`` kills the run at a named boundary and resumes it on a
fresh runtime, which is the quickest way to see the durability story rather
than read about it.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .evaluation.fixtures import load_fixtures
from .faults import CRASH_POINTS, CrashInjector, no_crash
from .llm.offline import OfflineSupportLLM
from .models import GateRequest, HumanDecision, Ticket
from .runtime import SupportRuntime, drive

DEMO_CLOCK = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m langgraph_production",
        description="Run one support ticket end to end against the offline stand in.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="ticket text to run")
    source.add_argument("--fixture", help="id of a ticket in the fixture file")
    parser.add_argument("--ticket-id", default="DEMO-1", help="id to use with --text")
    parser.add_argument(
        "--tier", default="pro", choices=["free", "pro", "enterprise"], help="account tier"
    )
    parser.add_argument("--approver", default="ops.dana", help="name recorded on the approval")
    parser.add_argument(
        "--deny", action="store_true", help="deny at the gate instead of approving"
    )
    parser.add_argument(
        "--crash-at",
        choices=CRASH_POINTS,
        help="kill the run at this boundary, then resume on a fresh runtime",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="keep checkpoints, effects and approvals in this file instead of a "
        "temporary one that is deleted on exit",
    )
    parser.add_argument("--json", action="store_true", help="print the final state as JSON")
    return parser


def _resolve_ticket(args: argparse.Namespace) -> Ticket:
    if args.text:
        return Ticket(id=args.ticket_id, text=args.text, account_tier=args.tier)
    for fixture in load_fixtures():
        if fixture.id == args.fixture:
            return fixture.to_ticket()
    raise SystemExit(f"no fixture with id {args.fixture!r}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ticket = _resolve_ticket(args)
    if args.db is not None:
        return _run(args, ticket, Path(args.db))
    with tempfile.TemporaryDirectory(prefix="langgraph-production-demo-") as workdir:
        return _run(args, ticket, Path(workdir) / "state.sqlite3")


def _run(args: argparse.Namespace, ticket: Ticket, db_path: Path) -> int:
    """One ticket, printed step by step, against the store at ``db_path``."""
    gates: list[GateRequest] = []

    def decider(gate: GateRequest) -> HumanDecision:
        gates.append(gate)
        print(f"\n  GATE ({gate.kind})")
        print(f"    token      {gate.token}")
        print(f"    state      {gate.state_digest}")
        if gate.action is not None:
            print(f"    action     {gate.action.summary}")
            print(f"    action id  {gate.action.action_id}")
        print(f"    question   {gate.question}")
        verdict = "deny" if args.deny else "approve"
        print(f"    decision   {verdict} by {args.approver}")
        return HumanDecision(
            token=gate.token,
            approver=args.approver,
            decision="deny" if args.deny else "approve",
            note="reviewed in the command line demo",
            decided_at=DEMO_CLOCK,
        )

    injector = CrashInjector(point=args.crash_at) if args.crash_at else None

    def factory(hook: Any) -> SupportRuntime:
        return SupportRuntime(
            llm=OfflineSupportLLM(),
            db_path=db_path,
            clock=lambda: DEMO_CLOCK,
            crash_hook=hook,
        )

    print(f"TICKET {ticket.id} ({ticket.account_tier})")
    print(f"  {ticket.text}")
    if args.crash_at:
        print(f"\n  crash injected at {args.crash_at}")

    outcome = drive(
        runtime_factory=factory,
        ticket=ticket,
        thread_id=f"demo-{ticket.id}",
        decider=decider,
        crash_hook=injector or no_crash,
    )
    state = outcome.result.state

    print(f"\n  PATH       {' -> '.join(outcome.result.trail)}")
    print(f"  OUTCOME    {outcome.result.outcome}")
    resumed = " (resumed from the checkpoint on disk)" if outcome.crashes else ""
    print(f"  CRASHES    {outcome.crashes}{resumed}")

    decision = state.get("decision") or {}
    print(f"  ROUTE      {decision.get('route')} at confidence {decision.get('confidence')}")
    if not decision.get("trusted", True):
        print(f"  FALLBACK   {decision.get('fallback_reason')}: {decision.get('rationale')}")

    receipt = state.get("receipt")
    if receipt:
        print(f"  EFFECT     {receipt['tool']} key {receipt['idempotency_key']}")
        print(f"             result {json.dumps(receipt['result'], sort_keys=True)}")
        print(f"             replayed from the store: {receipt['replayed']}")

    print(f"\n  REPLY      {outcome.result.reply}")

    inspector = SupportRuntime(llm=OfflineSupportLLM(), db_path=db_path)
    try:
        print(f"\n  handler executions recorded on disk: {inspector.effects.total_executions()}")
        print(f"  approval tokens consumed:            {inspector.approvals.count()}")
    finally:
        inspector.close()

    if args.json:
        print("\n" + json.dumps(state, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
