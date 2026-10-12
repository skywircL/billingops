"""``PathModel``: replays a task author's reference path, step by step.

The reference execution of an evaluation task proves that the environment can
serve the facts the task needs and that the expected outcome follows from
them. It must not encode any agent's policy, so this model knows nothing about
the business: it emits the author's tool calls in order, the author's
structured extraction for the extract step, and a fixed reply for the
conclusion step. When the path runs out it ends the investigation with
``insufficient_information`` so the run terminates and the check fails
visibly instead of hanging.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..tools import END_TOOL, EXTRACT_TOOL, FOLLOWUP_TOOL
from .base import Message, ModelConfig, ModelResponse, ToolCall, Usage

EXHAUSTED_STEP = {"tool": END_TOOL, "args": {"conclusion_type": "insufficient_information", "summary": "reference path exhausted", "facts": []}}


class PathModel:
    """Stateless: the step to emit is the number of assistant turns already in the transcript."""

    def __init__(self, *, extraction: dict[str, Any], path: Sequence[dict[str, Any]], reply: str | None = None,
                 followups: Sequence[str] | None = None) -> None:
        self.extraction = dict(extraction)
        # v1.2: the author's classification of each customer message seen while a proposal waits.
        self.followups = list(followups or [])
        self.followup_calls = 0
        self.path = [dict(s) for s in path]
        self.reply = reply or "Reference execution reply: see the business evidence for this case."
        self.calls = 0
        self.exhausted = False

    def generate(self, messages: Sequence[Message], tool_schemas: Sequence[dict[str, Any]], config: ModelConfig) -> ModelResponse:
        self.calls += 1
        n = sum(1 for m in messages if m.role == "assistant")
        if config.tag == "extract":
            return self._tool(EXTRACT_TOOL, self.extraction, n)
        if config.tag == "followup":
            kind = self.followups[self.followup_calls] if self.followup_calls < len(self.followups) else "unclear"
            self.followup_calls += 1
            return self._tool(FOLLOWUP_TOOL, {"kind": kind, "reason": "reference"}, n)
        if config.tag == "conclude":
            return ModelResponse(message=Message(role="assistant", content=self.reply), usage=Usage(input_tokens=0, output_tokens=0), stop_reason="end_turn", model="path")
        if n < len(self.path):
            step = self.path[n]
        else:
            self.exhausted = True
            step = EXHAUSTED_STEP
        return self._tool(str(step["tool"]), dict(step.get("args", {})), n)

    @staticmethod
    def _tool(name: str, args: dict[str, Any], n: int) -> ModelResponse:
        return ModelResponse(
            message=Message(role="assistant", tool_calls=[ToolCall(id=f"path-{n + 1}", name=name, arguments=args)]),
            usage=Usage(input_tokens=0, output_tokens=0),
            stop_reason="tool_use",
            model="path",
        )
