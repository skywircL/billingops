"""The graph.

Five nodes, three of which can be reached from the router, and exactly one
door to anything irreversible.

    classify -> answer            -> compose
    classify -> tool  -> plan     -> act -> compose
    classify -> tool  -> plan     -> human_gate -> act -> compose
    classify -> escalate          -> human_gate -> compose

The routing decision is data, not control flow: ``classify`` writes a decision
into the state and a conditional edge reads it. The gate is not optional for
irreversible tools, and no branch reaches ``act`` for one of them without
passing through it. That property is asserted in the test suite by walking the
compiled graph, not by reading this docstring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal

from langgraph.config import get_config
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from .approvals import (
    ApprovalLedger,
    AuthorizationError,
    InMemoryApprovalLedger,
    authorize,
    mint_token,
    parse_decision,
)
from .effects import EffectStore, InMemoryEffectStore, perform_effect
from .faults import CrashHook, no_crash
from .llm.base import LLMCallable, LLMRequest
from .models import (
    ApprovalRecord,
    GateRequest,
    HumanDecision,
    Outcome,
    PlannedAction,
    canonical_json,
    utc_now,
)
from .router import build_system_prompt, build_user_prompt, parse_router_output
from .state import (
    SupportState,
    read_action,
    read_approval,
    read_decision,
    state_digest,
)
from .tools import ToolRegistry

COMPOSER_SYSTEM_PROMPT = (
    "You write the reply a support customer receives. You are given the outcome of the "
    "triage as JSON. Use only what is in it. Do not promise anything it does not contain."
)

NODE_CLASSIFY = "classify"
NODE_PLAN = "plan"
NODE_GATE = "human_gate"
NODE_ACT = "act"
NODE_COMPOSE = "compose"


@dataclass
class GraphDeps:
    """Everything the graph reaches outside itself, in one injectable object."""

    llm: LLMCallable
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    effects: EffectStore = field(default_factory=InMemoryEffectStore)
    approvals: ApprovalLedger = field(default_factory=InMemoryApprovalLedger)
    clock: Callable[[], datetime] = utc_now
    min_confidence: float = 0.5
    crash_hook: CrashHook = no_crash


def current_thread_id() -> str:
    """Read the thread id off the running invocation.

    Taken from LangGraph's config rather than from the state, because the
    thread is the identity of the conversation and the namespace every
    idempotency key hangs off. A ticket replayed on a second thread is a
    second, separate effect, and that is the correct behaviour.
    """
    configurable = dict(get_config().get("configurable", {}))
    thread_id = configurable.get("thread_id")
    if not thread_id:
        raise ValueError("this graph requires a thread_id: it checkpoints and it resumes")
    return str(thread_id)


def _outcome(state: SupportState) -> Outcome:
    if state.get("gate_rejection"):
        return "gate_rejected"
    approval = read_approval(state)
    if approval is not None and approval.decision == "deny":
        return "denied"
    if state.get("receipt") is not None:
        return "acted" if approval is not None else "answered"
    if approval is not None and approval.kind == "handoff":
        return "handed_off"
    return "answered"


def build_graph(deps: GraphDeps) -> StateGraph:
    """Wire the nodes. Compilation and checkpointing belong to the runtime."""

    hook = deps.crash_hook

    # -- nodes -----------------------------------------------------------

    def classify(state: SupportState) -> dict[str, Any]:
        hook("classify:enter")
        raw = deps.llm(
            LLMRequest(
                tag="route",
                system=build_system_prompt(deps.registry),
                user=build_user_prompt(
                    ticket_id=state["ticket_id"],
                    text=state["text"],
                    account_tier=state.get("account_tier", "pro"),
                ),
            )
        )
        decision = parse_router_output(
            raw,
            registry=deps.registry,
            min_confidence=deps.min_confidence,
        )
        hook("classify:exit")
        return {"trail": [NODE_CLASSIFY], "decision": decision.model_dump(mode="json")}

    def plan(state: SupportState) -> dict[str, Any]:
        hook("plan:enter")
        decision = read_decision(state)
        if decision is None or decision.tool_call is None:
            raise ValueError("plan reached without a tool call, which the routing edge forbids")
        call = decision.tool_call
        action = PlannedAction.build(
            ticket_id=state["ticket_id"],
            call=call,
            spec=deps.registry.require(call.name),
        )
        hook("plan:exit")
        return {"trail": [NODE_PLAN], "action": action.model_dump(mode="json")}

    def human_gate(state: SupportState) -> dict[str, Any]:
        hook("gate:enter")
        thread_id = current_thread_id()
        action = read_action(state)
        kind = "approval" if action is not None else "handoff"
        digest_of_state = state_digest(state)
        token = mint_token(
            thread_id=thread_id,
            ticket_id=state["ticket_id"],
            kind=kind,
            action_id=action.action_id if action else None,
            state_digest=digest_of_state,
        )
        decision = read_decision(state)
        request = GateRequest(
            kind=kind,
            token=token,
            ticket_id=state["ticket_id"],
            state_digest=digest_of_state,
            action=action,
            question=(
                f"Approve {action.summary}? This cannot be undone."
                if action is not None
                else "This ticket needs a human. Take it over or close it without action."
            ),
            context={
                "text": state["text"],
                "account_tier": state.get("account_tier", "pro"),
                "router_rationale": decision.rationale if decision else "",
                "router_trusted": decision.trusted if decision else True,
                "router_fallback": decision.fallback_reason if decision else None,
            },
        )
        gate_payload = request.model_dump(mode="json")

        # Everything above this line runs again when the graph resumes: an
        # interrupted node restarts from the top. That is why the token is
        # derived from state rather than generated, and why nothing with a
        # side effect is allowed above this call.
        raw = interrupt(gate_payload)
        hook("gate:resumed")

        try:
            human = HumanDecision.model_validate(parse_decision(raw))
        except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            return {
                "trail": [NODE_GATE],
                "gate": gate_payload,
                "approval": None,
                "gate_rejection": f"malformed_decision: {exc}",
            }

        record = ApprovalRecord(
            token=human.token,
            ticket_id=state["ticket_id"],
            action_id=action.action_id if action else None,
            kind=kind,
            approver=human.approver,
            decision=human.decision,
            note=human.note,
            decided_at=human.decided_at,
            state_digest=digest_of_state,
        )

        try:
            # The return value says first use or replay. It belongs in the
            # ledger, which authorize writes to, and not in the state: a
            # resumed gate presents the same approval again, and a state field
            # that flipped would read as a resume that changed the outcome.
            authorize(record=record, expected_token=token, ledger=deps.approvals)
        except AuthorizationError as exc:
            return {
                "trail": [NODE_GATE],
                "gate": gate_payload,
                "approval": None,
                "gate_rejection": f"{exc.reason}: {exc.detail}",
            }

        return {
            "trail": [NODE_GATE],
            "gate": gate_payload,
            "approval": record.model_dump(mode="json"),
            "gate_rejection": None,
        }

    def act(state: SupportState) -> dict[str, Any]:
        hook("act:enter")
        action = read_action(state)
        if action is None:
            raise ValueError("act reached without a planned action")
        receipt = perform_effect(
            action=action,
            thread_id=current_thread_id(),
            registry=deps.registry,
            store=deps.effects,
            clock=deps.clock,
            hook=hook,
        )
        hook("act:after_effect")
        return {"trail": [NODE_ACT], "receipt": receipt.model_dump(mode="json")}

    def compose(state: SupportState) -> dict[str, Any]:
        hook("compose:enter")
        approval = read_approval(state)
        outcome = _outcome(state)
        context = {
            "ticket_id": state["ticket_id"],
            "outcome": outcome,
            "receipt": state.get("receipt"),
            "approver": approval.approver if approval else None,
            "note": approval.note if approval else None,
            "rejection": state.get("gate_rejection"),
        }
        reply = deps.llm(
            LLMRequest(
                tag="compose",
                system=COMPOSER_SYSTEM_PROMPT,
                user=canonical_json(context),
            )
        )
        hook("compose:exit")
        return {"trail": [NODE_COMPOSE], "reply": reply.strip(), "outcome": outcome}

    # -- edges -----------------------------------------------------------

    def after_classify(state: SupportState) -> Literal["plan", "human_gate", "compose"]:
        decision = read_decision(state)
        if decision is None:
            return NODE_GATE
        if decision.route == "tool":
            return NODE_PLAN
        if decision.route == "escalate":
            return NODE_GATE
        return NODE_COMPOSE

    def after_plan(state: SupportState) -> Literal["human_gate", "act"]:
        action = read_action(state)
        if action is None:
            raise ValueError("plan produced no action")
        # The only gate for irreversible work. Nothing else routes into act
        # for an irreversible tool.
        return NODE_GATE if action.irreversible else NODE_ACT

    def after_gate(state: SupportState) -> Literal["act", "compose"]:
        if state.get("gate_rejection"):
            return NODE_COMPOSE
        approval = read_approval(state)
        if approval is None or approval.decision != "approve":
            return NODE_COMPOSE
        return NODE_ACT if state.get("action") is not None else NODE_COMPOSE

    graph: StateGraph = StateGraph(SupportState)
    graph.add_node(NODE_CLASSIFY, classify)
    graph.add_node(NODE_PLAN, plan)
    graph.add_node(NODE_GATE, human_gate)
    graph.add_node(NODE_ACT, act)
    graph.add_node(NODE_COMPOSE, compose)

    graph.add_edge(START, NODE_CLASSIFY)
    graph.add_conditional_edges(
        NODE_CLASSIFY,
        after_classify,
        {NODE_PLAN: NODE_PLAN, NODE_GATE: NODE_GATE, NODE_COMPOSE: NODE_COMPOSE},
    )
    graph.add_conditional_edges(NODE_PLAN, after_plan, {NODE_GATE: NODE_GATE, NODE_ACT: NODE_ACT})
    graph.add_conditional_edges(NODE_GATE, after_gate, {NODE_ACT: NODE_ACT, NODE_COMPOSE: NODE_COMPOSE})
    graph.add_edge(NODE_ACT, NODE_COMPOSE)
    graph.add_edge(NODE_COMPOSE, END)
    return graph
