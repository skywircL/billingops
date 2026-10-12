"""Adapter from a LangChain chat model to :data:`LLMCallable`.

No provider SDK is imported here, and neither is ``langchain_core``. The
adapter needs one method, ``invoke``, and it passes messages in the
``(role, content)`` tuple form that every LangChain chat model accepts. That
keeps the runtime dependency list at langgraph and pydantic, and it means this
adapter can be exercised offline against a fake chat model, which is exactly
what the test suite does.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from .base import LLMCallable, LLMRequest


@runtime_checkable
class ChatModelLike(Protocol):
    """The slice of a chat model this package uses."""

    def invoke(self, input: Sequence[Any], /, *args: Any, **kwargs: Any) -> Any: ...


def _content_to_text(content: Any) -> str:
    """Flatten a message content field to text.

    Chat models return either a string or a list of content blocks. Both
    shapes show up in the wild, so both are handled instead of assumed.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


def from_chat_model(model: ChatModelLike) -> LLMCallable:
    """Wrap a chat model so the graph can take it.

    Args:
        model: anything with an ``invoke`` that accepts a message sequence.

    Returns:
        A callable the graph and the eval harness can use unchanged.
    """

    def call(request: LLMRequest) -> str:
        response = model.invoke([("system", request.system), ("user", request.user)])
        content = getattr(response, "content", response)
        return _content_to_text(content)

    return call
