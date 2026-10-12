"""The OpenAI-compatible adapter against a stub client. No network."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from billingops.llm.base import Message, ModelConfig, ModelError, ToolCall
from billingops.llm.openai_compat import OpenAICompatibleModel, to_chat_messages
from billingops.tools import INVESTIGATION_TOOL_SCHEMAS


class StubClient:
    def __init__(self, completion, fail_on_parallel: bool = False) -> None:
        self.calls: list[dict] = []
        self._completion = completion
        self._fail_on_parallel = fail_on_parallel
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail_on_parallel and "parallel_tool_calls" in kwargs:
            raise RuntimeError("unknown parameter parallel_tool_calls")
        if isinstance(self._completion, Exception):
            raise self._completion
        return self._completion


def _completion(content=None, tool_calls=(), finish="tool_use", cache_hit=0):
    message = SimpleNamespace(content=content, tool_calls=[SimpleNamespace(id=i, function=SimpleNamespace(name=n, arguments=a)) for i, n, a in tool_calls])
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=SimpleNamespace(prompt_tokens=300, completion_tokens=40, prompt_cache_hit_tokens=cache_hit), model="deepseek-flash")


def test_request_shape_disables_parallel_calls_and_parses_tool_calls() -> None:
    client = StubClient(_completion(tool_calls=[("call_1", "get_invoice", '{"invoice_id": "INV-1"}')], cache_hit=200))
    model = OpenAICompatibleModel(client=client)
    out = model.generate([Message(role="system", content="s"), Message(role="user", content="u")], INVESTIGATION_TOOL_SCHEMAS, ModelConfig(tag="investigate", model="deepseek-flash"))
    kw = client.calls[0]
    assert kw["model"] == "deepseek-flash" and kw["parallel_tool_calls"] is False and kw["tool_choice"] == "auto" and kw["temperature"] == 0.0
    assert kw["tools"][0]["type"] == "function" and "parameters" in kw["tools"][0]["function"]
    assert out.tool_calls == [ToolCall("call_1", "get_invoice", {"invoice_id": "INV-1"})]
    assert out.usage.cache_read_input_tokens == 200 and out.usage.input_tokens == 100 and out.usage.output_tokens == 40


def test_endpoint_without_the_parallel_flag_is_retried_once_without_it() -> None:
    client = StubClient(_completion(content="hi", finish="stop"), fail_on_parallel=True)
    model = OpenAICompatibleModel(client=client)
    out = model.generate([Message(role="user", content="u")], INVESTIGATION_TOOL_SCHEMAS, ModelConfig(tag="investigate", model="m"))
    assert len(client.calls) == 2 and "parallel_tool_calls" not in client.calls[1]
    assert out.message.content == "hi"
    model.generate([Message(role="user", content="u")], INVESTIGATION_TOOL_SCHEMAS, ModelConfig(tag="investigate", model="m"))
    assert len(client.calls) == 3  # remembered: no third attempt with the flag


def test_history_round_trips_and_unparseable_arguments_are_kept_visible() -> None:
    history = [
        Message(role="assistant", tool_calls=[ToolCall("call_9", "get_invoice", {"invoice_id": "INV-1"})]),
        Message(role="tool", tool_call_id="call_9", content='{"ok": true}'),
    ]
    chat = to_chat_messages(history)
    assert chat[0]["tool_calls"][0]["function"] == {"name": "get_invoice", "arguments": '{"invoice_id": "INV-1"}'}
    assert chat[1] == {"role": "tool", "tool_call_id": "call_9", "content": '{"ok": true}'}
    client = StubClient(_completion(tool_calls=[("c", "get_invoice", "{not json")]))
    out = OpenAICompatibleModel(client=client).generate([Message(role="user", content="u")], [], ModelConfig(tag="investigate", model="m"))
    assert "__unparseable__" in out.tool_calls[0].arguments


def test_provider_errors_become_model_errors() -> None:
    with pytest.raises(ModelError):
        OpenAICompatibleModel(client=StubClient(RuntimeError("boom"))).generate([Message(role="user", content="u")], [], ModelConfig(tag="conclude", model="m"))


def test_an_empty_assistant_turn_is_serialised_as_a_visible_marker() -> None:
    chat = to_chat_messages([Message(role="assistant", content="", tool_calls=[]), Message(role="user", content="continue")])
    assert chat[0] == {"role": "assistant", "content": "(empty response)"}
    with_call = to_chat_messages([Message(role="assistant", content="", tool_calls=[ToolCall("c1", "list_invoices", {})])])
    assert with_call[0]["content"] is None and with_call[0]["tool_calls"]
