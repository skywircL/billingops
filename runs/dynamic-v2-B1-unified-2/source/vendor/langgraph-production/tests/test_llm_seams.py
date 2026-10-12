"""The cassette and the chat model adapter, both exercised without a network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

import pytest

from langgraph_production.llm.base import LLMRequest
from langgraph_production.llm.cassette import Cassette, CassetteMiss, recording, replaying
from langgraph_production.llm.chat_model import from_chat_model

ROUTE = LLMRequest(tag="route", system="you triage", user="refund INV-10032")
OTHER = LLMRequest(tag="route", system="you triage", user="where is ORD-40021")


class CountingModel:
    """Stands in for the paid thing. Counts calls so a test can prove it stopped."""

    def __init__(self, response: str = "recorded") -> None:
        self.response = response
        self.calls = 0

    def __call__(self, request: LLMRequest) -> str:
        self.calls += 1
        return self.response


class FakeChatModel:
    """Shaped like a LangChain chat model: invoke(messages) returns a message."""

    def __init__(self, content: Any) -> None:
        self.content = content
        self.seen: list[Sequence[Any]] = []

    def invoke(self, input: Sequence[Any], /, *args: Any, **kwargs: Any) -> Any:
        self.seen.append(list(input))
        return type("Message", (), {"content": self.content})()


def test_request_keys_are_stable() -> None:
    assert ROUTE.key() == LLMRequest(tag="route", system="you triage", user="refund INV-10032").key()


def test_request_keys_separate_different_prompts() -> None:
    assert ROUTE.key() != OTHER.key()


def test_request_keys_separate_call_sites() -> None:
    compose = LLMRequest(tag="compose", system=ROUTE.system, user=ROUTE.user)
    assert ROUTE.key() != compose.key()


def test_recording_writes_a_file(tmp_path: Path) -> None:
    cassette = Cassette(tmp_path / "tape.json")
    inner = CountingModel()
    call = recording(inner, cassette)
    assert call(ROUTE) == "recorded"
    assert (tmp_path / "tape.json").exists()
    assert len(Cassette(tmp_path / "tape.json")) == 1


def test_recording_does_not_pay_twice_for_the_same_request(tmp_path: Path) -> None:
    cassette = Cassette(tmp_path / "tape.json")
    inner = CountingModel()
    call = recording(inner, cassette)
    call(ROUTE)
    call(ROUTE)
    assert inner.calls == 1


def test_recording_can_be_told_to_call_every_time(tmp_path: Path) -> None:
    cassette = Cassette(tmp_path / "tape.json")
    inner = CountingModel()
    call = recording(inner, cassette, reuse=False)
    call(ROUTE)
    call(ROUTE)
    assert inner.calls == 2


def test_a_cassette_replays_without_the_model(tmp_path: Path) -> None:
    path = tmp_path / "tape.json"
    inner = CountingModel("the recorded answer")
    recording(inner, Cassette(path))(ROUTE)

    replay = replaying(Cassette(path))
    assert replay(ROUTE) == "the recorded answer"
    assert inner.calls == 1


def test_a_replay_miss_raises_instead_of_calling_out(tmp_path: Path) -> None:
    cassette = Cassette(tmp_path / "tape.json")
    with pytest.raises(CassetteMiss):
        replaying(cassette)(ROUTE)


def test_a_replay_miss_can_fall_back_when_asked(tmp_path: Path) -> None:
    inner = CountingModel("live")
    assert replaying(Cassette(tmp_path / "tape.json"), fallback=inner)(ROUTE) == "live"


def test_a_cassette_stores_the_prompt_for_a_reviewer(tmp_path: Path) -> None:
    path = tmp_path / "tape.json"
    recording(CountingModel(), Cassette(path))(ROUTE)
    stored = json.loads(path.read_text(encoding="utf-8"))
    entry = next(iter(stored.values()))
    assert entry["user"] == ROUTE.user
    assert entry["tag"] == "route"


def test_membership_reads_naturally(tmp_path: Path) -> None:
    path = tmp_path / "tape.json"
    recording(CountingModel(), Cassette(path))(ROUTE)
    cassette = Cassette(path)
    assert ROUTE in cassette
    assert OTHER not in cassette


def test_the_adapter_sends_system_and_user_messages() -> None:
    model = FakeChatModel("ok")
    from_chat_model(model)(ROUTE)
    assert model.seen[0] == [("system", ROUTE.system), ("user", ROUTE.user)]


def test_the_adapter_returns_plain_text() -> None:
    assert from_chat_model(FakeChatModel("plain"))(ROUTE) == "plain"


def test_the_adapter_flattens_content_blocks() -> None:
    model = FakeChatModel([{"type": "text", "text": "part one "}, {"type": "text", "text": "part two"}])
    assert from_chat_model(model)(ROUTE) == "part one part two"


def test_the_adapter_flattens_a_list_of_strings() -> None:
    assert from_chat_model(FakeChatModel(["a", "b"]))(ROUTE) == "ab"


def test_the_adapter_copes_with_a_model_that_returns_bare_text() -> None:
    class BareModel:
        def invoke(self, input: Sequence[Any], /, *args: Any, **kwargs: Any) -> Any:
            return "bare"

    assert from_chat_model(BareModel())(ROUTE) == "bare"


def test_the_adapter_works_against_a_real_langchain_fake() -> None:
    """Proof the tuple message form is the right shape, not just our own fake."""
    pytest.importorskip("langchain_core")
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    model = GenericFakeChatModel(messages=iter([AIMessage(content='{"route": "answer"}')]))
    assert json.loads(from_chat_model(model)(ROUTE))["route"] == "answer"
