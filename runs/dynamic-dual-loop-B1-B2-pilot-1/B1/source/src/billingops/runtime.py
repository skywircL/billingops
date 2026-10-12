"""Owns the graph, the durable stores, and the task loop.

One process, one executor. Tasks go ``pending -> running -> done`` and are
claimed one at a time. While a case waits for a user or an approver the
executor holds nothing: the graph is checkpointed, the task is finished, and
the next command creates a new task.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from langgraph_production.approvals import SqliteApprovalLedger
from langgraph_production.effects import SqliteEffectStore
from langgraph_production.faults import CrashHook, SimulatedCrash, no_crash
from langgraph_production.models import utc_now

from .billing_client import BillingClient
from .cases.store import CaseStore, new_id
from .config import Settings
from .execution.refund import FINAL_STATUSES, ExecutionDeps, action_key, batch_followup_kind, execute_approved, execute_batch, ledger_view, reconcile_batch_commands, verify
from .investigation.graph import GraphDeps, build_graph
from .investigation.state import fresh_budgets
from .llm.base import Model
from .observability.tracing import Tracer, ObservedModel, ObservedTools
from .tools import ToolRunner

log = logging.getLogger("billingops.runtime")


class StateConflict(RuntimeError):
    pass


class CaseRuntime:
    def __init__(
        self,
        *,
        settings: Settings,
        model: Model,
        billing: BillingClient | None = None,
        clock: Callable[[], datetime] = utc_now,
        crash_hook: CrashHook = no_crash,
        tracer: Tracer | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.crash_hook = crash_hook
        self.tracer = tracer or Tracer(enabled=False)
        Path(settings.app_db).parent.mkdir(parents=True, exist_ok=True)
        self.store = CaseStore(settings.app_db)
        self._checkpoint_conn = sqlite3.connect(settings.app_db, check_same_thread=False)
        self.checkpointer = SqliteSaver(self._checkpoint_conn)
        self.effects = SqliteEffectStore(settings.app_db)
        self.approvals = SqliteApprovalLedger(settings.app_db)
        self._owns_billing = billing is None
        self.billing = billing or BillingClient(settings.billing_url, admin_token=settings.billing_admin_token)
        self.model = model
        self.deps = GraphDeps(
            model=ObservedModel(model, lambda: self.tracer),
            tool_runner_factory=lambda customer_id: ObservedTools(ToolRunner(self.billing, customer_id), lambda: self.tracer),
            approvals=self.approvals,
            clock=clock,
            crash_hook=crash_hook,
            on_event=self._graph_event,
            agent_revision=settings.agent_revision,
        )
        self.graph = build_graph(self.deps).compile(checkpointer=self.checkpointer)
        self.execution = ExecutionDeps(store=self.store, effects=self.effects, billing=self.billing, clock=clock, crash_hook=crash_hook, agent_revision=settings.agent_revision)
        self._executor: threading.Thread | None = None
        self._stop = threading.Event()
        self.pid = os.getpid()

    # -- lifecycle -----------------------------------------------------------------

    def close(self) -> None:
        self.stop_executor()
        closers = [self.effects.close, self.approvals.close, self._checkpoint_conn.close]
        if self._owns_billing:
            closers.append(self.billing.close)
        for closer in closers:
            try:
                closer()
            except Exception:
                pass
        self.tracer.flush()

    def recover_on_startup(self) -> int:
        """Confirm the previous executor is gone, then requeue whatever it left running."""
        if not self.store.acquire_runtime_lock(self.pid):
            raise RuntimeError("another executor is alive on this database; refusing to start a second one")
        requeued = self.store.requeue_running_tasks()
        if requeued:
            log.info("requeued %d task(s) left running by a previous process", requeued)
        return requeued

    def start_executor(self, *, poll_interval_s: float = 0.2) -> None:
        self.recover_on_startup()
        self._stop.clear()

        def loop() -> None:
            last_beat = 0.0
            while not self._stop.is_set():
                if time.monotonic() - last_beat > 5:
                    self.store.heartbeat(self.pid)
                    last_beat = time.monotonic()
                if not self.run_one_task():
                    self._stop.wait(poll_interval_s)

        self._executor = threading.Thread(target=loop, name="billingops-executor", daemon=True)
        self._executor.start()

    def stop_executor(self) -> None:
        self._stop.set()
        if self._executor is not None:
            self._executor.join(timeout=10)
            self._executor = None
        try:
            self.store.release_runtime_lock(self.pid)
        except Exception:
            pass

    def drain(self, *, timeout_s: float = 60.0) -> None:
        """Run tasks inline until the queue is empty. Tests and the harness use this instead of the thread."""
        deadline = time.monotonic() + timeout_s
        while self.run_one_task():
            if time.monotonic() > deadline:
                raise TimeoutError("task queue did not drain")

    # -- tasks --------------------------------------------------------------------------

    def run_one_task(self) -> bool:
        task = self.store.claim_next_task()
        if task is None:
            return False
        try:
            with self.tracer.task(self.store, task):
                self.handle_task(task)
        except SimulatedCrash:
            # The process "died". The task stays running, exactly as a real crash would leave it.
            raise
        except Exception as exc:
            log.error("task %s failed: %s", task["id"], traceback.format_exc())
            self.store.finish_task(task["id"], status="failed", error=f"{type(exc).__name__}: {exc}")
            self.store.append_event(task["case_id"], "task_failed", "system", {"task": task["id"], "kind": task["kind"], "error": str(exc)})
            case = self.store.get_case(task["case_id"])
            if case and case["state"] not in ("closed", "escalated", "uncertain", "verifying", "executing"):
                self.store.set_state(task["case_id"], "error", error=str(exc))
            return True
        self.store.finish_task(task["id"])
        return True

    def handle_task(self, task: dict[str, Any]) -> None:
        case_id = task["case_id"]
        kind = task["kind"]
        payload = task["payload"]
        case = self.store.get_case(case_id)
        if case is None:
            return

        command_id = payload.get("command_id")
        snapshot = self._snapshot(case_id)
        duplicate = bool(command_id) and command_id in (snapshot.get("consumed_commands") or [])

        # 1. Reconcile with the effect ledger first. An open claim means a send
        #    may have happened; nothing but verification is allowed, whatever
        #    the task was. A duplicate command is still recorded as such.
        if case.get("action_key"):
            row = self.effects.get(case["action_key"])
            if row is not None:
                if duplicate:
                    self.store.append_event(case_id, "duplicate_command_ignored", "system", {"command_id": command_id, "task_kind": kind})
                if row.get("tool") == "refund_batch":
                    execute_batch(self.execution, case, case["proposal"], actor=payload.get("actor", "system"),
                                  **({"verification_attempts": 1} if kind == "verify" else {}))
                    return
                if row.get("status") not in FINAL_STATUSES:
                    if kind == "verify":
                        verify(self.execution, case_id, case["action_key"], case["proposal"], attempts=1, actor=payload.get("actor", "system"))
                    else:
                        self.store.append_event(case_id, "recovery_verify_first", "system", {"task_kind": kind, "ledger_status": row.get("status")})
                        execute_approved(self.execution, case, case["proposal"], actor="system")
                else:
                    execute_approved(self.execution, case, case["proposal"], actor="system")  # reconciles the case, sends nothing
                return

        if kind == "verify":
            # Nothing to verify: no claim was ever made. Explicit recovery from error is a graph resume.
            if case["state"] == "error":
                self._resume_graph(case, None)
            return

        # 2. A command consumed once is never consumed again.
        if duplicate:
            self.store.append_event(case_id, "duplicate_command_ignored", "system", {"command_id": command_id, "task_kind": kind})
            return

        # 3. Drive the graph from its checkpoint.
        if kind == "start":
            if snapshot:
                self._resume_graph(case, None)  # a restart mid-investigation: continue from the checkpoint
            else:
                self._start_graph(case)
            return
        if kind in ("message", "decision"):
            if not self._graph_is_interrupted(case_id):
                # Nothing is waiting for this command; it cannot be applied. Recorded, not consumed.
                self.store.append_event(case_id, "command_not_applicable", "system", {"command_id": command_id, "task_kind": kind})
                self._resume_graph(case, None)
                return
            self._resume_graph(case, {"type": kind, **payload})
            return
        raise ValueError(f"unknown task kind {kind}")

    # -- graph driving -----------------------------------------------------------------------

    @staticmethod
    def _config(case_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": case_id}, "recursion_limit": 200}

    def _snapshot(self, case_id: str) -> dict[str, Any]:
        try:
            return dict(self.graph.get_state(self._config(case_id)).values)
        except Exception:
            return {}

    def _graph_is_interrupted(self, case_id: str) -> bool:
        try:
            snapshot = self.graph.get_state(self._config(case_id))
        except Exception:
            return False
        return bool(snapshot.next) and any(getattr(t, "interrupts", ()) for t in snapshot.tasks)

    def _start_graph(self, case: dict[str, Any]) -> None:
        payload = {
            "case_id": case["id"],
            "customer_id": case["customer_id"],
            "text": case["text"],
            "mode": case["mode"],
            "trail": [],
            "budgets": fresh_budgets(),
            "messages": [],
            "evidence": [],
            "usage": [],
            "clarifications": [],
            "consumed_commands": [],
            "proposal_version": 0,
        }
        self.store.append_event(case["id"], "run_started", "system", {"mode": case["mode"]})
        raw = self.graph.invoke(payload, self._config(case["id"]), durability="sync")
        self._interpret(case, raw)

    def _resume_graph(self, case: dict[str, Any], resume_payload: dict[str, Any] | None) -> None:
        if resume_payload is None:
            raw = self.graph.invoke(None, self._config(case["id"]), durability="sync")
        else:
            raw = self.graph.invoke(Command(resume=resume_payload), self._config(case["id"]), durability="sync")
        self._interpret(case, raw)

    def _interpret(self, case: dict[str, Any], raw: dict[str, Any]) -> None:
        case_id = case["id"]
        interrupts = raw.get("__interrupt__") or []
        state = {k: v for k, v in raw.items() if not k.startswith("__")}
        conclusion = state.get("conclusion")
        if interrupts:
            value = interrupts[0].value
            if value.get("kind") == "clarify":
                self.store.set_state(case_id, "awaiting_user", pending_question=value["question"])
                self.store.append_event(case_id, "clarification_asked", "system", {"question": value["question"], "budgets": value.get("budgets")})
                return
            if value.get("kind") == "approval":
                proposal = value["proposal"]
                # A progress question returns to the same gate: same proposal, same token. Say so
                # instead of logging a second approval request an approver might act on twice.
                same_request = case.get("gate_token") == value["token"] and int(case.get("proposal_version") or 0) == int(proposal["version"])
                self.store.set_state(
                    case_id,
                    "awaiting_approval",
                    proposal=proposal,
                    proposal_version=proposal["version"],
                    gate_token=value["token"],
                    conclusion=conclusion,
                    final_message=(conclusion or {}).get("text"),
                    pending_question=None,
                )
                if same_request:
                    self.store.append_event(case_id, "approval_still_pending", "system", {"proposal_version": proposal["version"], "token": value["token"]})
                else:
                    request = {"proposal_version": proposal["version"], "amount_cents": proposal["amount_cents"], "payment_id": proposal["payment_id"], "token": value["token"]}
                    if proposal.get("kind") == "batch_refund":
                        request.update(kind="batch_refund", invoice_id=proposal["invoice_id"], items=proposal["items"])
                    self.store.append_event(case_id, "approval_requested", "system", request)
                return
            raise ValueError(f"unknown interrupt {value}")

        outcome = state.get("outcome")
        if outcome == "approved":
            proposal = state["proposal"]
            self.store.update_case(case_id, proposal=proposal, proposal_version=proposal["version"], conclusion=conclusion)
            self.store.set_state(case_id, "executing")
            execute_approved(self.execution, self.store.get_case(case_id), proposal, actor="system")
            return
        if outcome == "explained":
            self.store.set_state(case_id, "closed", proposal=state.get("proposal"), gate_token=None, conclusion=conclusion, final_message=(conclusion or {}).get("text"), pending_question=None)
            return
        if outcome == "withdrawn":
            # Spec 5.2 (v1.2): the customer withdrew while the proposal waited. Nothing was sent;
            # the case closes and no later decision can apply (closed accepts no commands).
            self.store.set_state(case_id, "closed", proposal=state.get("proposal"), gate_token=None, conclusion=conclusion, final_message=(conclusion or {}).get("text"), pending_question=None)
            return
        if outcome == "rejected":
            approval = state.get("approval") or {}
            self.store.set_state(case_id, "closed", conclusion=conclusion, final_message=f"The refund proposal was not approved ({approval.get('note') or 'no note'}). No refund was issued.", pending_question=None)
            return
        if outcome == "escalated":
            escalation = {"reason": state.get("escalation_reason"), "by": "system", "at": self.clock().isoformat(), "original_action": "investigation"}
            self.store.set_state(case_id, "escalated", proposal=state.get("proposal"), gate_token=None, conclusion=conclusion, final_message=(conclusion or {}).get("text"), escalation=escalation, pending_question=None)
            self.store.append_event(case_id, "escalated", "system", escalation)
            return
        if outcome == "error":
            self.store.set_state(case_id, "error", error=str(state.get("escalation_reason")), conclusion=conclusion)
            return
        raise ValueError(f"graph ended without an outcome: trail={state.get('trail')}")

    def _graph_event(self, case_id: str, kind: str, payload: dict[str, Any]) -> None:
        self.store.append_event(case_id, kind, "system", payload)

    # -- commands (called by the API layer after persistence) -----------------------------------

    def submit_case(self, *, customer_id: str, created_by: str, text: str, mode: str | None = None) -> dict[str, Any]:
        case_id = new_id("case")
        case = self.store.create_case(case_id=case_id, customer_id=customer_id, created_by=created_by, mode=mode or self.settings.mode, text=text)
        self.store.append_event(case_id, "case_submitted", created_by, {"customer_id": customer_id, "mode": case["mode"]})
        self.store.enqueue_task(case_id, "start", {})
        return case

    def submit_message(self, case_id: str, *, text: str, actor: str) -> str:
        from .cases.store import GRAPH_MESSAGE_STATES

        case = self._require_state(case_id, "message")
        # The batch executor uses the same short lock immediately before a new
        # claim. Once this command is durable, an unclaimed item cannot pass it.
        with self.execution.command_lock:
            command_id = self.store.record_command(case_id=case_id, kind="message", payload={"text": text}, actor=actor)
        self.store.append_event(case_id, "message_received", actor, {"command_id": command_id, "length": len(text)})
        if case["state"] not in GRAPH_MESSAGE_STATES:
            if (case.get("proposal") or {}).get("kind") == "batch_refund":
                kind = batch_followup_kind(text)
                key = case.get("action_key")
                if key and (self.effects.get(key) or {}).get("tool") == "refund_batch":
                    root = reconcile_batch_commands(self.execution, case_id, key, actor=actor)
                    current = self.store.get_case(case_id) or case
                    reply = current.get("final_message") if root.get("stop_request") else progress_reply(current, self.effects, text)
                elif kind == "progress":
                    reply = progress_reply(case, self.effects, text)
                else:
                    reply = ("已记录您的撤回或范围变更。旧提案尚未发出的退款将停止，已经取得执行凭据的款项只能继续核验；后续需要人工复核。"
                             if _CJK_RE.search(text) else "Your withdrawal or scope change has been recorded. Remaining unclaimed refunds will stop; already claimed items can only be verified. Further action requires review.")
                self.store.append_event(case_id, "customer_reply", "system", {"kind": kind, "state": (self.store.get_case(case_id) or case)["state"], "text": reply, "command_id": command_id})
                return command_id
            # Spec 5.5 (v1.2): a progress question. Answered from the record; no task, no state change.
            reply = progress_reply(case, self.effects, text)
            self.store.append_event(case_id, "customer_reply", "system", {"kind": "progress", "state": case["state"], "text": reply, "command_id": command_id})
            return command_id
        if case["state"] == "awaiting_approval":
            self.store.set_state(case_id, "investigating", actor=actor)
        else:
            self.store.set_state(case_id, "investigating", actor=actor, pending_question=None)
        self.store.enqueue_task(case_id, "message", {"command_id": command_id, "text": text})
        return command_id

    def submit_decision(self, case_id: str, *, decision: str, approver: str, note: str, proposal_version: int, token: str | None) -> str:
        case = self._require_state(case_id, "decision")
        if int(case["proposal_version"]) != int(proposal_version):
            raise StateConflict(f"proposal version {proposal_version} is not the current version {case['proposal_version']}")
        expected = case.get("gate_token")
        if token is not None and token != expected:
            raise StateConflict("approval token does not match the current proposal")
        payload = {
            "token": expected,
            "approver": approver,
            "decision": decision,
            "note": note,
            "decided_at": self.clock().isoformat(),
            "proposal_version": int(proposal_version),
        }
        command_id = self.store.record_command(case_id=case_id, kind="decision", payload=payload, actor=approver)
        self.store.append_event(case_id, "decision_received", approver, {"command_id": command_id, "decision": decision, "proposal_version": proposal_version})
        self.store.set_state(case_id, "investigating" if decision == "approve" else "investigating", actor=approver)
        self.store.enqueue_task(case_id, "decision", {"command_id": command_id, **payload})
        return command_id

    def submit_resume(self, case_id: str, *, actor: str) -> str:
        case = self._require_state(case_id, "resume")
        command_id = self.store.record_command(case_id=case_id, kind="resume", payload={}, actor=actor)
        self.store.append_event(case_id, "resume_requested", actor, {"command_id": command_id, "state": case["state"]})
        self.store.enqueue_task(case_id, "verify", {"command_id": command_id, "actor": actor})
        return command_id

    def submit_escalation(self, case_id: str, *, operator: str, reason: str) -> str:
        case = self._require_state(case_id, "escalate")
        command_id = self.store.record_command(case_id=case_id, kind="escalate", payload={"reason": reason}, actor=operator)
        escalation = {
            "reason": reason,
            "by": operator,
            "at": self.clock().isoformat(),
            "original_action": "refund" if case.get("action_key") else "investigation",
            "from_state": case["state"],
            "ledger_status": (self.effects.get(case["action_key"]) or {}).get("status") if case.get("action_key") else None,
        }
        # The claim, the approval, the key and the ledger row are untouched: an
        # operator takes over the case, not the money.
        self.store.set_state(case_id, "escalated", actor=operator, escalation=escalation, pending_question=None)
        self.store.append_event(case_id, "escalated", operator, escalation)
        return command_id

    def reply_for(self, case_id: str, command_id: str) -> str | None:
        """The progress reply a message produced, if it has one yet (awaiting_approval replies arrive after the task runs)."""
        for event in reversed(self.store.events(case_id)):
            if event["kind"] == "customer_reply" and event["payload"].get("command_id") == command_id:
                return str(event["payload"].get("text"))
        return None

    def _require_state(self, case_id: str, command: str) -> dict[str, Any]:
        from .cases.store import ALLOWED_COMMANDS

        case = self.store.get_case(case_id)
        if case is None:
            raise LookupError(case_id)
        if command not in ALLOWED_COMMANDS.get(case["state"], frozenset()):
            raise StateConflict(f"{command} is not allowed while the case is {case['state']}")
        return case

    # -- views ------------------------------------------------------------------------------------

    def case_view(self, case_id: str) -> dict[str, Any] | None:
        case = self.store.get_case(case_id)
        if case is None:
            return None
        snapshot = self._snapshot(case_id)
        return {
            "case": case,
            "execution": ledger_view(self.effects, case.get("action_key")),
            "budgets": snapshot.get("budgets"),
            "evidence": snapshot.get("evidence", []),
            "clarifications": snapshot.get("clarifications", []),
            "usage": snapshot.get("usage", []),
            "protocol_errors": snapshot.get("protocol_errors", []),
            "blocked_proposals": snapshot.get("blocked_proposals", []),
            "invalidated_proposals": snapshot.get("invalidated_proposals", []),
            "trail": snapshot.get("trail", []),
            "open_tasks": self.store.pending_tasks(case_id),
        }


_CJK_RE = __import__("re").compile(r"[\u4e00-\u9fff]")

_PROGRESS_TEXT = {
    # state: (zh, en). Spec 5.5: never claim money has arrived, never claim refunded or not refunded while unknown.
    "investigating": ("我们正在核查您的账单记录，结果出来后会在这里答复您。", "We are checking your billing records and will reply here with the result."),
    "executing": ("退款已获批准，正在执行；到账时间以支付渠道为准，暂时无法承诺。", "The refund has been approved and is being issued; we cannot promise when it will arrive."),
    "verifying": ("退款已获批准，我们正在核实执行结果；核实前无法确认是否已到账。", "The refund has been approved and we are confirming the result; until then we cannot say whether it has arrived."),
    "uncertain": ("退款的执行结果还在核实，已交由人工跟进；在核实之前，我们不能告诉您已退或未退。", "The outcome of the refund is still being confirmed and a staff member is following up; until then we cannot say whether it was refunded."),
    "error": ("处理过程中出现问题，已交由人工跟进。", "Something went wrong while handling your case; a staff member is following up."),
    "escalated": ("您的案件已转交人工处理，工作人员会在这里答复您。", "Your case has been passed to a staff member, who will reply here."),
}


def progress_reply(case: dict[str, Any], effects: Any, text: str) -> str:
    """A factual status line built from the case record; no model, no reads, no writes."""
    zh, en = _PROGRESS_TEXT.get(case["state"], _PROGRESS_TEXT["escalated"])
    return zh if _CJK_RE.search(text or "") else en
