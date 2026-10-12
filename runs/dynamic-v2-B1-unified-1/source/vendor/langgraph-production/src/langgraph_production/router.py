"""Prompt construction and the strict parser behind the routing node.

The parser is the interesting half. It never raises, whatever it is handed,
and it never guesses: any output it cannot fully trust becomes an escalation
with a named reason, and
that reason lands in the state where the eval harness and a human reviewer can
both see it. Nine distinct failure modes are enumerated in
:data:`langgraph_production.models.RouterFallback`, and each one has a test.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import Route, RouterDecision, RouterFallback, ToolCall
from .tools import ToolRegistry, validation_errors

VALID_ROUTES: tuple[Route, ...] = ("answer", "tool", "escalate")

FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

ROUTER_SYSTEM_PROMPT = """You triage support tickets for a business software product.

Choose exactly one route:
- "answer": the ticket is a general product question you can answer from documentation.
- "tool": the ticket asks about or asks for a change to a specific record, and you have every identifier the tool needs.
- "escalate": a human must handle it. Use this when the ticket carries legal, security, or commercial weight, when it asks for more than one irreversible change at once, or when an identifier the tool requires is missing.

Tools available:
{catalogue}

Rules:
- Never invent an identifier. If the ticket does not contain it, escalate.
- Tools marked IRREVERSIBLE still require human approval after you choose them. Choosing one is a proposal, not an execution.
- Reply with a single JSON object and nothing else:
  {{"route": "...", "tool": "... or omit", "args": {{}}, "confidence": 0.0, "rationale": "one sentence"}}
"""


def build_system_prompt(registry: ToolRegistry) -> str:
    return ROUTER_SYSTEM_PROMPT.format(catalogue=registry.catalogue())


def build_user_prompt(*, ticket_id: str, text: str, account_tier: str) -> str:
    return (
        f"Ticket id: {ticket_id}\n"
        f"Account tier: {account_tier}\n"
        f"Message:\n{text}\n"
    )


def _escalate(reason: RouterFallback, detail: str, confidence: float = 0.0) -> RouterDecision:
    return RouterDecision(
        route="escalate",
        tool_call=None,
        confidence=confidence,
        rationale=detail,
        trusted=False,
        fallback_reason=reason,
    )


def _strip_fence(raw: str) -> str:
    match = FENCE_PATTERN.search(raw)
    return match.group(1).strip() if match else raw.strip()


def parse_router_output(
    raw: object,
    *,
    registry: ToolRegistry,
    min_confidence: float = 0.5,
) -> RouterDecision:
    """Turn model text into a decision, or into a named refusal.

    Args:
        raw: whatever the model returned. Typed as ``object`` rather than
            ``str`` because "the callable returns a string" is a contract, and
            a parser that only cannot raise while the contract holds is not
            the thing this module claims to be. Anything that is not text is
            rendered as text and refused like any other unparseable output.
        registry: the tools the model is allowed to name.
        min_confidence: below this the decision is not acted on.

    Returns:
        A :class:`RouterDecision`. When ``trusted`` is False the route is
        always ``escalate`` and ``fallback_reason`` says which check failed.
    """
    text = raw if isinstance(raw, str) else str(raw)
    if not text.strip():
        return _escalate("empty_output", "the model returned nothing")

    try:
        parsed: Any = json.loads(_strip_fence(text))
    except json.JSONDecodeError as exc:
        return _escalate("invalid_json", f"output was not JSON: {exc.msg}")

    if not isinstance(parsed, dict):
        return _escalate("not_an_object", f"output was a {type(parsed).__name__}, expected an object")

    route = parsed.get("route")
    if route not in VALID_ROUTES:
        return _escalate("unknown_route", f"route {route!r} is not one of {list(VALID_ROUTES)}")

    raw_confidence = parsed.get("confidence", 0.0)
    if not isinstance(raw_confidence, (int, float)) or isinstance(raw_confidence, bool):
        return _escalate("invalid_confidence", f"confidence {raw_confidence!r} is not a number")
    confidence = float(raw_confidence)
    if not 0.0 <= confidence <= 1.0:
        return _escalate("invalid_confidence", f"confidence {confidence} is outside 0..1")

    rationale = str(parsed.get("rationale", "")).strip()

    if route != "tool":
        if confidence < min_confidence and route == "answer":
            # A low confidence answer is the one that quietly ships a wrong
            # fact to a customer. Escalating is cheaper than being wrong.
            return _escalate(
                "low_confidence",
                f"answer confidence {confidence} below threshold {min_confidence}",
                confidence=confidence,
            )
        return RouterDecision(route=route, confidence=confidence, rationale=rationale)

    tool_name = parsed.get("tool")
    if not isinstance(tool_name, str) or not tool_name:
        return _escalate("missing_tool", "route was 'tool' but no tool was named")

    spec = registry.get(tool_name)
    if spec is None:
        return _escalate("unknown_tool", f"tool {tool_name!r} is not in the registry")

    args = parsed.get("args", {})
    if not isinstance(args, dict):
        return _escalate("invalid_tool_args", f"args was a {type(args).__name__}, expected an object")

    problem = validation_errors(spec, args)
    if problem is not None:
        return _escalate("invalid_tool_args", f"{tool_name} arguments rejected: {problem}")

    if confidence < min_confidence:
        return _escalate(
            "low_confidence",
            f"tool confidence {confidence} below threshold {min_confidence}",
            confidence=confidence,
        )

    return RouterDecision(
        route="tool",
        tool_call=ToolCall(name=tool_name, args=dict(args)),
        confidence=confidence,
        rationale=rationale,
    )
