"""The seven required demonstrations, end to end, against a live Billing service.

    uv run python scripts/demo.py                # scripted stand-in
    uv run python scripts/demo.py --model openai --only 1,2

Each demonstration starts from a fresh application database and a fresh copy
of the same Billing world. The output is the story the video follows.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from langgraph_production.faults import CrashInjector, SimulatedCrash

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from billingops.billing_client import BillingClient  # noqa: E402
from billingops.config import Settings  # noqa: E402
from billingops.evaluation.experiment import build_model  # noqa: E402
from billingops.execution.refund import execute_approved  # noqa: E402
from billingops.runtime import CaseRuntime  # noqa: E402
from billingops.testing import BillingServer, ambiguous_world, duplicate_world  # noqa: E402

DUP_TEXT = "I was charged twice for invoice INV-2026-0142 (49.00 yuan each). Please refund the duplicate."


def say(line: str = "") -> None:
    print(line, flush=True)


def show(rt: CaseRuntime, case_id: str, label: str) -> dict:
    view = rt.case_view(case_id)
    case, execution = view["case"], view["execution"]
    exec_s = f"ledger={execution['status']} sends={execution['sends_recorded']} verifications={execution['verify_attempts']}" if execution else "ledger=-"
    say(f"  [{label}] state={case['state']:18s} proposal=v{case['proposal_version']} {exec_s}")
    return view


class Demo:
    def __init__(self, model_spec: str, mode: str) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="billingops-demo-"))
        self.server = BillingServer(self.tmp / "billing.sqlite3").start()
        self.billing = BillingClient(self.server.url, admin_token="demo-admin")
        self.model_spec = model_spec
        self.mode = mode
        self.n = 0

    def fresh(self, *, crash_hook=None, world=None) -> CaseRuntime:
        self.n += 1
        self.billing.load_world(world or duplicate_world())
        settings = Settings(app_db=str(self.tmp / f"app-{self.n}.sqlite3"), billing_url=self.server.url, mode=self.mode, model=self.model_spec)
        kwargs = {"crash_hook": crash_hook} if crash_hook else {}
        return CaseRuntime(settings=settings, model=build_model(self.model_spec), billing=self.billing, **kwargs)

    def same_db(self, n: int) -> CaseRuntime:
        settings = Settings(app_db=str(self.tmp / f"app-{n}.sqlite3"), billing_url=self.server.url, mode=self.mode, model=self.model_spec)
        return CaseRuntime(settings=settings, model=build_model(self.model_spec), billing=self.billing)

    def refunds(self) -> str:
        rows = self.billing.payment_refunds("C-1001", "PAY-88213")
        return f"{len(rows)} refund(s) on PAY-88213: {[r['amount_cents'] for r in rows]}"

    def close(self) -> None:
        self.billing.close()
        self.server.stop()

    # -- 1 -----------------------------------------------------------------------------------

    def demo1(self) -> None:
        say("\n=== 1. Investigate a duplicate charge, approve, refund, verify ===")
        rt = self.fresh()
        case = rt.submit_case(customer_id="C-1001", created_by="cust-c1001", text=DUP_TEXT)
        rt.drain()
        view = show(rt, case["id"], "after investigation")
        say(f"  reads: {[(e['tool'], e['args']) for e in view['evidence']]}")
        say(f"  proposal: refund {view['case']['proposal']['amount_cents']} cents on {view['case']['proposal']['payment_id']}, dispute key {view['case']['proposal']['dispute_key']}")
        say(f"  to customer: {view['case']['final_message']}")
        rt.submit_decision(case["id"], decision="approve", approver="ops-dana", note="checked the invoice", proposal_version=view["case"]["proposal_version"], token=None)
        rt.drain()
        view = show(rt, case["id"], "after approval")
        say(f"  to customer: {view['case']['final_message']}")
        say(f"  billing: {self.refunds()}")
        rt.close()

    def demo2(self) -> None:
        say("\n=== 2. Missing information: pause, answer, resume ===")
        # January and February were each paid twice: the target is ambiguous without asking.
        # The real model asks on most runs, not all; up to three attempts are made and counted.
        for attempt in range(1, 4):
            rt = self.fresh(world=ambiguous_world())
            case = rt.submit_case(customer_id="C-1001", created_by="cust-c1001", text="You charged me twice, please refund one of them.")
            rt.drain()
            if rt.store.get_case(case["id"])["state"] == "awaiting_user":
                break
            say(f"  attempt {attempt}: the model picked an invoice without asking (allowed, not what this demo shows); retrying")
            rt.close()
        view = show(rt, case["id"], f"paused (attempt {attempt})" if rt.store.get_case(case["id"])["state"] == "awaiting_user" else "not paused")
        if view["case"]["state"] != "awaiting_user":
            say("  no pause in three attempts; see the recorded trace data/replays/real/clarification_then_investigation.json")
            rt.close()
            return
        say(f"  question to customer: {view['case']['pending_question']}")
        say(f"  executor released: open tasks = {len(view['open_tasks'])}")
        rt.submit_message(case["id"], text="The February one, invoice INV-2026-0142.", actor="cust-c1001")
        rt.drain()
        view = show(rt, case["id"], "resumed")
        say(f"  budgets: {view['budgets']}")
        rt.close()

    def demo3(self) -> None:
        say("\n=== 3. Write succeeds, response lost, process dies; restart verifies, one refund ===")
        rt = self.fresh(crash_hook=CrashInjector("refund:after_send"))
        case = rt.submit_case(customer_id="C-1001", created_by="cust-c1001", text=DUP_TEXT)
        rt.drain()
        c = rt.store.get_case(case["id"])
        rt.submit_decision(case["id"], decision="approve", approver="ops-dana", note="", proposal_version=c["proposal_version"], token=None)
        try:
            rt.drain()
        except SimulatedCrash:
            say("  process crashed right after the POST left")
        show(rt, case["id"], "as the dead process left it")
        say(f"  billing: {self.refunds()}")
        rt.close()
        rt2 = self.same_db(self.n)
        say(f"  restart: requeued {rt2.recover_on_startup()} task(s)")
        rt2.drain()
        show(rt2, case["id"], "after recovery")
        say(f"  billing: {self.refunds()}   sends ever recorded: {rt2.effects.total_executions()}")
        rt2.close()

    def demo4(self) -> None:
        say("\n=== 4. A proposal change invalidates the old approval ===")
        rt = self.fresh()
        case = rt.submit_case(customer_id="C-1001", created_by="cust-c1001", text=DUP_TEXT)
        rt.drain()
        c = rt.store.get_case(case["id"])
        old_version, old_token = c["proposal_version"], c["gate_token"]
        say(f"  proposal v{old_version}, token {old_token[:12]}...")
        rt.submit_message(case["id"], text="Wait, please also check the previous month, I think the same happened.", actor="cust-c1001")
        rt.drain()
        c = rt.store.get_case(case["id"])
        say(f"  proposal v{c['proposal_version']}, token {c['gate_token'][:12]}... (old token dead: {c['gate_token'] != old_token})")
        try:
            rt.submit_decision(case["id"], decision="approve", approver="ops-dana", note="", proposal_version=old_version, token=old_token)
        except Exception as exc:
            say(f"  approving v{old_version}: refused -> {exc}")
        rt.submit_decision(case["id"], decision="approve", approver="ops-dana", note="", proposal_version=c["proposal_version"], token=None)
        rt.drain()
        show(rt, case["id"], "after approving the current version")
        say(f"  billing: {self.refunds()}")
        rt.close()

    def demo5_6(self) -> None:
        say("\n=== 5. Verification unavailable: stay uncertain, operator takes over ===")
        rt = self.fresh()
        case = rt.submit_case(customer_id="C-1001", created_by="cust-c1001", text=DUP_TEXT)
        rt.drain()
        c = rt.store.get_case(case["id"])
        self.billing.arm_faults(verification_unavailable=10)
        rt.submit_decision(case["id"], decision="approve", approver="ops-dana", note="", proposal_version=c["proposal_version"], token=None)
        rt.drain()
        view = show(rt, case["id"], "after 3 failed verifications")
        say(f"  last verification: {view['execution']['last_verification']['result']}")
        rt.submit_escalation(case["id"], operator="ops-dana", reason="verification endpoint down, taking over")
        view = show(rt, case["id"], "after handoff")
        say(f"  escalation keeps the claim: ledger_status={view['case']['escalation']['ledger_status']}")
        say("\n=== 6. After handoff, a late success is found and the case closes ===")
        self.billing.clear_faults()
        rt.submit_resume(case["id"], actor="ops-dana")
        rt.drain()
        view = show(rt, case["id"], "after one more verification")
        say(f"  receipt: refund {view['execution']['receipt']['refund']['id']} for {view['execution']['receipt']['amount_cents']} cents")
        say(f"  billing: {self.refunds()}   sends ever recorded: {rt.effects.total_executions()}")
        rt.close()

    def demo7(self) -> None:
        say("\n=== 7. Persisted terminal rejection: confirmed not executed, handed off ===")
        rt = self.fresh()
        case = rt.submit_case(customer_id="C-1001", created_by="cust-c1001", text=DUP_TEXT)
        rt.drain()
        c = rt.store.get_case(case["id"])
        # Between approval and execution someone else refunds the payment in full. Billing must refuse.
        world = self.billing.dump_world()
        world["refunds"].append({"id": "RF-EXT", "payment_id": "PAY-88213", "customer_id": "C-1001", "amount_cents": 4900, "currency": "CNY", "status": "completed", "reason": "manual refund by finance", "created_at": "2026-03-01T00:00:00", "completed_at": "2026-03-01T00:00:00", "request_key": None})
        proposal = dict(c["proposal"])
        rt.store.update_case(case["id"], state="executing")
        self.billing.load_world(world)
        row = execute_approved(rt.execution, rt.store.get_case(case["id"]), proposal)
        view = show(rt, case["id"], "after Billing refused")
        say(f"  rejection: {row['receipt']['rejection_code']} -> escalation reason {view['case']['escalation']['reason']}")
        say(f"  billing: {self.refunds()} (the external one; ours was never executed)")
        rt.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="fake", help="fake | openai (the pinned DeepSeek model)")
    parser.add_argument("--mode", default="dynamic", choices=["fixed", "dynamic"])
    parser.add_argument("--only", default="1,2,3,4,5,7")
    args = parser.parse_args()
    wanted = {int(x) for x in args.only.split(",")}
    demo = Demo(args.model, args.mode)
    started = time.monotonic()
    try:
        if 1 in wanted:
            demo.demo1()
        if 2 in wanted:
            demo.demo2()
        if 3 in wanted:
            demo.demo3()
        if 4 in wanted:
            demo.demo4()
        if 5 in wanted or 6 in wanted:
            demo.demo5_6()
        if 7 in wanted:
            demo.demo7()
    finally:
        demo.close()
    say(f"\ndone in {time.monotonic() - started:.1f}s with model={args.model} mode={args.mode}")


if __name__ == "__main__":
    main()
