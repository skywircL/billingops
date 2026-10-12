"""Every way the router can be wrong, and the fact that none of them guess."""

from __future__ import annotations

import json

import pytest

from langgraph_production.router import parse_router_output
from langgraph_production.tools import ToolRegistry


def parse(raw: object, registry: ToolRegistry, **kwargs: float):
    return parse_router_output(raw, registry=registry, **kwargs)  # type: ignore[arg-type]


def test_a_clean_answer_is_trusted(registry: ToolRegistry) -> None:
    decision = parse(json.dumps({"route": "answer", "confidence": 0.9, "rationale": "docs"}), registry)
    assert decision.route == "answer"
    assert decision.trusted is True
    assert decision.fallback_reason is None


def test_a_clean_tool_call_carries_its_arguments(registry: ToolRegistry) -> None:
    decision = parse(
        json.dumps(
            {
                "route": "tool",
                "tool": "issue_refund",
                "args": {"invoice_ref": "INV-10032", "amount_cents": 4500},
                "confidence": 0.9,
            }
        ),
        registry,
    )
    assert decision.tool_call is not None
    assert decision.tool_call.args["amount_cents"] == 4500


def test_a_fenced_json_block_is_unwrapped(registry: ToolRegistry) -> None:
    raw = '```json\n{"route": "escalate", "confidence": 0.8}\n```'
    assert parse(raw, registry).route == "escalate"


def test_a_fenced_block_without_a_language_tag_is_unwrapped(registry: ToolRegistry) -> None:
    raw = '```\n{"route": "escalate", "confidence": 0.8}\n```'
    assert parse(raw, registry).route == "escalate"


@pytest.mark.parametrize("raw", ["", "   ", "\n\t "])
def test_empty_output_escalates(registry: ToolRegistry, raw: str) -> None:
    decision = parse(raw, registry)
    assert decision.route == "escalate"
    assert decision.fallback_reason == "empty_output"


def test_prose_instead_of_json_escalates(registry: ToolRegistry) -> None:
    decision = parse("Sure, I have refunded that for you.", registry)
    assert decision.fallback_reason == "invalid_json"
    assert decision.route == "escalate"


def test_a_json_list_escalates(registry: ToolRegistry) -> None:
    assert parse('["answer"]', registry).fallback_reason == "not_an_object"


def test_an_unknown_route_escalates(registry: ToolRegistry) -> None:
    decision = parse(json.dumps({"route": "refund_it", "confidence": 0.99}), registry)
    assert decision.fallback_reason == "unknown_route"


def test_a_missing_route_escalates(registry: ToolRegistry) -> None:
    assert parse(json.dumps({"confidence": 0.9}), registry).fallback_reason == "unknown_route"


def test_route_tool_without_a_tool_escalates(registry: ToolRegistry) -> None:
    decision = parse(json.dumps({"route": "tool", "confidence": 0.9}), registry)
    assert decision.fallback_reason == "missing_tool"


def test_an_invented_tool_escalates(registry: ToolRegistry) -> None:
    decision = parse(
        json.dumps({"route": "tool", "tool": "wire_transfer", "args": {}, "confidence": 0.99}),
        registry,
    )
    assert decision.fallback_reason == "unknown_tool"
    assert decision.tool_call is None


def test_an_invented_identifier_escalates(registry: ToolRegistry) -> None:
    decision = parse(
        json.dumps(
            {
                "route": "tool",
                "tool": "issue_refund",
                "args": {"invoice_ref": "the may invoice", "amount_cents": 4500},
                "confidence": 0.99,
            }
        ),
        registry,
    )
    assert decision.fallback_reason == "invalid_tool_args"
    assert "invoice_ref" in decision.rationale


def test_args_of_the_wrong_shape_escalate(registry: ToolRegistry) -> None:
    decision = parse(
        json.dumps({"route": "tool", "tool": "lookup_invoice", "args": "INV-10032", "confidence": 0.9}),
        registry,
    )
    assert decision.fallback_reason == "invalid_tool_args"


def test_a_negative_refund_escalates(registry: ToolRegistry) -> None:
    decision = parse(
        json.dumps(
            {
                "route": "tool",
                "tool": "issue_refund",
                "args": {"invoice_ref": "INV-10032", "amount_cents": -4500},
                "confidence": 0.95,
            }
        ),
        registry,
    )
    assert decision.fallback_reason == "invalid_tool_args"


@pytest.mark.parametrize("value", ["high", None, True, [0.9]])
def test_a_non_numeric_confidence_escalates(registry: ToolRegistry, value: object) -> None:
    decision = parse(json.dumps({"route": "answer", "confidence": value}), registry)
    assert decision.fallback_reason == "invalid_confidence"


@pytest.mark.parametrize("value", [-0.1, 1.5, 42])
def test_a_confidence_outside_the_unit_range_escalates(registry: ToolRegistry, value: float) -> None:
    decision = parse(json.dumps({"route": "answer", "confidence": value}), registry)
    assert decision.fallback_reason == "invalid_confidence"


def test_a_low_confidence_answer_escalates(registry: ToolRegistry) -> None:
    decision = parse(json.dumps({"route": "answer", "confidence": 0.2}), registry)
    assert decision.fallback_reason == "low_confidence"
    assert decision.route == "escalate"


def test_a_low_confidence_tool_call_escalates(registry: ToolRegistry) -> None:
    decision = parse(
        json.dumps(
            {
                "route": "tool",
                "tool": "cancel_subscription",
                "args": {"account_ref": "ACC-2041"},
                "confidence": 0.3,
            }
        ),
        registry,
    )
    assert decision.fallback_reason == "low_confidence"
    assert decision.tool_call is None


def test_the_confidence_threshold_is_configurable(registry: ToolRegistry) -> None:
    raw = json.dumps({"route": "answer", "confidence": 0.6})
    assert parse(raw, registry, min_confidence=0.5).trusted is True
    assert parse(raw, registry, min_confidence=0.9).fallback_reason == "low_confidence"


def test_a_low_confidence_escalation_is_still_an_escalation(registry: ToolRegistry) -> None:
    decision = parse(json.dumps({"route": "escalate", "confidence": 0.1}), registry)
    assert decision.route == "escalate"
    assert decision.trusted is True


def test_the_parser_never_raises(registry: ToolRegistry) -> None:
    for raw in ["", "{", "null", "[]", "0", '{"route": {}}', '{"route": "tool", "tool": 7}']:
        assert parse(raw, registry).route in {"answer", "tool", "escalate"}


@pytest.mark.parametrize("raw", [4500, None, [1, 2], b'{"route": "answer"}', {"route": "answer"}])
def test_the_parser_never_raises_on_anything_that_is_not_text(
    registry: ToolRegistry, raw: object
) -> None:
    """"It never raises" has to survive a callable that did not return a string.

    A chat model wrapper handing back a token count, a None, or raw bytes is a
    plausible integration bug, and the parser turning it into an AttributeError
    would take down the node that exists to keep the run on its feet.
    """
    decision = parse(raw, registry)
    assert decision.route == "escalate"
    assert decision.trusted is False
    assert decision.fallback_reason is not None


def test_every_fallback_lands_on_escalate(registry: ToolRegistry) -> None:
    samples = ["", "not json", "[]", '{"route": "x"}', '{"route": "tool"}']
    assert {parse(raw, registry).route for raw in samples} == {"escalate"}
