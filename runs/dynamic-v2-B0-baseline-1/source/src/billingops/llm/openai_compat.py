"""OpenAI-compatible chat-completions adapter: DeepSeek V4.1 Flash (``deepseek-flash``) per ``.env``.

The model seam: neutral messages in, one
``ModelResponse`` out. Parallel tool calls are disabled at the API
(``parallel_tool_calls=False``); the application enforces the single-tool rule
again regardless. Sampling is fixed at temperature 0.
"""

from __future__ import annotations

import json
import os
from typing import Any, Sequence

from ..config import MODEL_BASE_URL, MODEL_TEMPERATURE
from .base import Message, ModelConfig, ModelError, ModelResponse, ToolCall, ToolSchema, Usage


def to_chat_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "system":
            out.append({"role": "system", "content": msg.content})
        elif msg.role == "user":
            out.append({"role": "user", "content": msg.content})
        elif msg.role == "assistant":
            # A turn with neither text nor tool calls (reasoning only, or truncated) is
            # invalid on the wire; it is sent as a visible empty marker so the transcript
            # stays well formed and the model sees that the turn produced nothing.
            content = msg.content if msg.content else (None if msg.tool_calls else "(empty response)")
            item: dict[str, Any] = {"role": "assistant", "content": content}
            for block in msg.provider_content or []:
                # Reasoning models return reasoning_content that must travel back with the turn.
                if isinstance(block, dict) and block.get("reasoning_content") is not None:
                    item["reasoning_content"] = block["reasoning_content"]
            if msg.tool_calls:
                item["tool_calls"] = [
                    {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                    for tc in msg.tool_calls
                ]
            out.append(item)
        elif msg.role == "tool":
            out.append({"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content})
        else:
            raise ValueError(f"unknown role {msg.role}")
    return out


def to_chat_tools(tool_schemas: Sequence[ToolSchema]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": s["name"], "description": s.get("description", ""), "parameters": s["input_schema"]}}
        for s in tool_schemas
    ]


def from_chat_completion(completion: Any) -> ModelResponse:
    choice = completion.choices[0]
    message = choice.message
    calls: list[ToolCall] = []
    for tc in message.tool_calls or []:
        raw = tc.function.arguments or "{}"
        try:
            arguments = json.loads(raw)
        except json.JSONDecodeError:
            arguments = {"__unparseable__": raw}
        if not isinstance(arguments, dict):
            arguments = {"__unparseable__": raw}
        calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=arguments))
    usage = completion.usage
    cache_hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    reasoning = getattr(message, "reasoning_content", None)
    return ModelResponse(
        message=Message(
            role="assistant",
            content=message.content or "",
            tool_calls=calls,
            provider_content=[{"reasoning_content": reasoning}] if reasoning else None,
        ),
        usage=Usage(
            input_tokens=max(0, prompt - cache_hit),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cache_read_input_tokens=cache_hit,
            cache_creation_input_tokens=0,
        ),
        stop_reason=str(choice.finish_reason or ""),
        model=str(getattr(completion, "model", "") or ""),
    )


class OpenAICompatibleModel:
    """``openai`` SDK client pointed at the configured base URL."""

    def __init__(self, *, client: Any | None = None, max_retries: int = 2, timeout_s: float = 120.0) -> None:
        if client is None:
            from openai import OpenAI

            api_key = os.environ.get("BILLINGOPS_LLM_API_KEY")
            if not api_key:
                raise ModelError("BILLINGOPS_LLM_API_KEY is not set; see .env.example")
            client = OpenAI(api_key=api_key, base_url=MODEL_BASE_URL, max_retries=max_retries, timeout=timeout_s)
        self._client = client
        self._supports_parallel_flag = True
        self.per_call_timeout_s: float | None = None
        """Set by the trial runner to the remaining task deadline before each call."""

    def generate(self, messages: Sequence[Message], tool_schemas: Sequence[ToolSchema], config: ModelConfig) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": config.model,
            "messages": to_chat_messages(messages),
            "max_tokens": config.max_tokens,
            "temperature": MODEL_TEMPERATURE,
        }
        if tool_schemas:
            kwargs["tools"] = to_chat_tools(tool_schemas)
            kwargs["tool_choice"] = "auto"
            if self._supports_parallel_flag and config.disable_parallel_tool_use:
                kwargs["parallel_tool_calls"] = False
        client = self._client if self.per_call_timeout_s is None else self._client.with_options(timeout=self.per_call_timeout_s)
        try:
            completion = client.chat.completions.create(**kwargs)
        except Exception as exc:
            # An endpoint that rejects the parallel flag is asked once more without it;
            # the application layer still refuses multi-call turns.
            if "parallel_tool_calls" in str(exc) and "parallel_tool_calls" in kwargs:
                self._supports_parallel_flag = False
                kwargs.pop("parallel_tool_calls")
                try:
                    completion = client.chat.completions.create(**kwargs)
                except Exception as exc2:
                    raise provider_error(exc2) from exc2
            else:
                raise provider_error(exc) from exc
        if not completion.choices:
            raise ModelError("empty completion")
        return from_chat_completion(completion)


def provider_error(exc: Exception) -> ModelError:
    """Wrap an SDK exception, keeping its class and HTTP status as fields for classification."""
    status = getattr(exc, "status_code", None)
    return ModelError(f"{type(exc).__name__}: {exc}", provider_error=type(exc).__name__, status=status if isinstance(status, int) else None)
