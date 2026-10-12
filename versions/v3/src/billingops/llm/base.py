"""The model seam: ``generate(messages, tool_schemas, model_config)``.

Everything above this line speaks in plain, JSON-serialisable messages that
carry no provider objects. The one concession is ``provider_content`` on an
assistant message: adaptive thinking returns signed blocks that must be sent
back verbatim on the next turn, so the adapter keeps them opaque here and the
replay normaliser ignores them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol, Sequence

Role = Literal["system", "user", "assistant", "tool"]
Tag = Literal["extract", "investigate", "conclude"]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False
    provider_content: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [asdict(tc) for tc in self.tool_calls]
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.is_error:
            out["is_error"] = True
        if self.provider_content is not None:
            out["provider_content"] = self.provider_content
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Message":
        return cls(
            role=raw["role"],
            content=raw.get("content", ""),
            tool_calls=[ToolCall(**tc) for tc in raw.get("tool_calls", [])],
            tool_call_id=raw.get("tool_call_id"),
            is_error=bool(raw.get("is_error", False)),
            provider_content=raw.get("provider_content"),
        )


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class ModelConfig:
    tag: Tag
    model: str
    max_tokens: int = 4096
    effort: str = "medium"
    disable_parallel_tool_use: bool = True
    """Requested from the provider when supported. The application layer
    still enforces one tool call per turn regardless."""


@dataclass
class ModelResponse:
    message: Message
    usage: Usage
    stop_reason: str = ""
    model: str = ""

    @property
    def tool_calls(self) -> list[ToolCall]:
        return list(self.message.tool_calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message.to_dict(),
            "usage": self.usage.as_dict(),
            "stop_reason": self.stop_reason,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ModelResponse":
        return cls(
            message=Message.from_dict(raw["message"]),
            usage=Usage(**raw.get("usage", {})),
            stop_reason=raw.get("stop_reason", ""),
            model=raw.get("model", ""),
        )


ToolSchema = dict[str, Any]
"""``{"name", "description", "input_schema"}`` in JSON-schema form."""


class Model(Protocol):
    def generate(
        self, messages: Sequence[Message], tool_schemas: Sequence[ToolSchema], config: ModelConfig
    ) -> ModelResponse: ...


class ModelError(RuntimeError):
    """The provider failed in a way the caller should count as a runtime failure."""
