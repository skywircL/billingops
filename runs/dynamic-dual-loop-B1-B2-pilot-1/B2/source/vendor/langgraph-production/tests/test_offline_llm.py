"""The offline stand in is part of the product surface, so it is tested too."""

from __future__ import annotations

import json

import pytest

from langgraph_production.evaluation.fixtures import load_fixtures
from langgraph_production.llm.base import LLMRequest
from langgraph_production.llm.offline import MalformedLLM, OfflineSupportLLM, ScriptedLLM
from langgraph_production.router import parse_router_output
from langgraph_production.tools import ToolRegistry


def route(llm: OfflineSupportLLM, text: str) -> dict[str, object]:
    raw = llm(LLMRequest(tag="route", system="", user=text))
    return json.loads(raw)


def test_every_routing_response_is_valid_json_for_every_fixture() -> None:
    llm = OfflineSupportLLM()
    for fixture in load_fixtures():
        payload = route(llm, fixture.text)
        assert payload["route"] in {"answer", "tool", "escalate"}


def test_every_routing_response_survives_the_strict_parser() -> None:
    llm = OfflineSupportLLM()
    registry = ToolRegistry()
    for fixture in load_fixtures():
        raw = llm(LLMRequest(tag="route", system="", user=fixture.text))
        decision = parse_router_output(raw, registry=registry)
        assert decision.fallback_reason is None, f"{fixture.id}: {decision.rationale}"


def test_the_stand_in_is_deterministic() -> None:
    first, second = OfflineSupportLLM(), OfflineSupportLLM()
    text = "Please refund $45.00 from invoice INV-10032."
    assert route(first, text) == route(second, text)


def test_a_security_incident_outranks_every_intent_rule() -> None:
    llm = OfflineSupportLLM()
    payload = route(llm, "Our security team says ACC-2041 credentials leaked, cancel it now.")
    assert payload["route"] == "escalate"


def test_two_irreversible_asks_in_one_ticket_escalate() -> None:
    llm = OfflineSupportLLM()
    payload = route(llm, "Cancel the renewal on ACC-2155 and refund $300.00 from INV-10188.")
    assert payload["route"] == "escalate"
    assert "more than one irreversible" in str(payload["rationale"])


def test_a_refund_without_an_amount_escalates() -> None:
    payload = route(OfflineSupportLLM(), "Please refund INV-10077.")
    assert payload["route"] == "escalate"


def test_a_cancellation_without_an_account_escalates() -> None:
    payload = route(OfflineSupportLLM(), "Cancel everything immediately.")
    assert payload["route"] == "escalate"


@pytest.mark.parametrize(
    "text,cents",
    [
        ("Refund $45.00 from INV-10032", 4500),
        ("Refund $19.90 on INV-10120", 1990),
        ("Refund $300 against INV-10188", 30000),
        ("Refund $1,200.50 from INV-10077", 120050),
    ],
)
def test_amounts_are_read_in_cents(text: str, cents: int) -> None:
    payload = route(OfflineSupportLLM(), text)
    assert payload["args"] == {"invoice_ref": payload["args"]["invoice_ref"], "amount_cents": cents}


def test_the_composer_uses_only_what_it_is_given() -> None:
    llm = OfflineSupportLLM()
    reply = llm(
        LLMRequest(
            tag="compose",
            system="",
            user=json.dumps({"outcome": "denied", "note": "outside the refund window"}),
        )
    )
    assert "outside the refund window" in reply


def test_the_composer_survives_a_payload_it_cannot_read() -> None:
    reply = OfflineSupportLLM()(LLMRequest(tag="compose", system="", user="not json"))
    assert reply


def test_scripted_llm_records_what_it_was_asked() -> None:
    llm = ScriptedLLM({"route": '{"route": "answer", "confidence": 0.9}'}, default="hello")
    assert json.loads(llm(LLMRequest(tag="route", system="s", user="u")))["route"] == "answer"
    assert llm(LLMRequest(tag="compose", system="s", user="u")) == "hello"
    assert [call.tag for call in llm.calls] == ["route", "compose"]


def test_malformed_llm_produces_something_the_parser_rejects() -> None:
    llm = MalformedLLM()
    decision = parse_router_output(
        llm(LLMRequest(tag="route", system="", user="refund it")), registry=ToolRegistry()
    )
    assert decision.route == "escalate"
    assert decision.trusted is False
