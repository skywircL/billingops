"""The one type the rest of the package depends on."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

Tag = Literal["route", "compose"]
"""Which call site a request comes from. Cassettes and fakes key off this."""


@dataclass(frozen=True)
class LLMRequest:
    """A single model call, fully described by data.

    Frozen and hashable so it can be used as a cassette key without any
    provider specific object leaking into the store.
    """

    tag: Tag
    system: str
    user: str

    def key(self) -> str:
        from ..models import digest

        return digest({"tag": self.tag, "system": self.system, "user": self.user})


LLMCallable = Callable[[LLMRequest], str]
"""Everything the graph needs from a language model: text in, text out."""
