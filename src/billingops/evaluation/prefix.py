"""Limited prefix diagnostic: replay a recorded read-only investigation prefix, then run a candidate suffix.

    prefix-run --artifact runs/<run>/artifacts/<task>-B-t0.json --switch-at 3 --model openai --out runs/prefix/<name>

The first ``switch_at`` investigation turns come verbatim from the recorded
transcript (assistant tool calls); the environment produces the tool results
live and every result must equal the recorded one, otherwise the run stops as
``unreproducible``. There is no fallback to the live model inside the prefix.
The switch point must lie before the recorded ``end_investigation`` and the
task must carry no fault plan; approval, execution and faults are not part of
a v1 prefix. Prefix turns cost nothing; the suffix is a normal live run and is
reported separately from end-to-end results.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from ..llm.base import Message, ModelConfig, ModelResponse, ToolCall, Usage
from ..tools import END_TOOL, EXTRACT_TOOL
from .tasks import EvalTask, utc_now


class Unreproducible(RuntimeError):
    """The live environment diverged from the recording inside the prefix."""


def _normalise_tool_content(content: str) -> Any:
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return content


class PrefixModel:
    """Replays recorded assistant turns up to the switch point, verifying tool results turn by turn."""

    def __init__(self, *, snapshot: dict[str, Any], switch_at: int, live: Any) -> None:
        self.extracted = dict(snapshot.get("extracted") or {})
        self.recorded = [Message.from_dict(m) for m in snapshot.get("messages", [])]
        self.switch_at = int(switch_at)
        self.live = live
        self.replayed = 0
        self.verified_tool_results = 0
        self.switched = False
        self.log: list[dict[str, Any]] = []
        assistant_turns = [m for m in self.recorded if m.role == "assistant"]
        ends = [i for i, m in enumerate(assistant_turns) if any(c.name == END_TOOL for c in m.tool_calls)]
        limit = ends[0] if ends else len(assistant_turns)
        if not 0 < self.switch_at <= limit:
            raise ValueError(f"switch_at must be in 1..{limit} (turns before the recorded end_investigation)")

    def _verify(self, messages: Sequence[Message]) -> None:
        live_tools = [m for m in messages if m.role == "tool"]
        rec_tools = [m for m in self.recorded if m.role == "tool"]
        for i, live in enumerate(live_tools):
            if i >= len(rec_tools):
                raise Unreproducible(f"live transcript has more tool results ({len(live_tools)}) than the recording")
            if _normalise_tool_content(live.content) != _normalise_tool_content(rec_tools[i].content):
                raise Unreproducible(f"tool result {i + 1} differs from the recording")
        self.verified_tool_results = len(live_tools)

    def generate(self, messages: Sequence[Message], tool_schemas: Sequence[dict[str, Any]], config: ModelConfig) -> ModelResponse:
        if config.tag == "extract":
            self.log.append({"tag": "extract", "source": "replay"})
            return ModelResponse(message=Message(role="assistant", tool_calls=[ToolCall(id="prefix-extract", name=EXTRACT_TOOL, arguments=self.extracted)]), usage=Usage(input_tokens=0, output_tokens=0), stop_reason="tool_use", model="prefix")
        if config.tag != "investigate":
            return self.live.generate(messages, tool_schemas, config)
        n = sum(1 for m in messages if m.role == "assistant")
        if n < self.switch_at:
            self._verify(messages)
            turn = [m for m in self.recorded if m.role == "assistant"][n]
            calls = [ToolCall(id=f"prefix-{n + 1}-{i}", name=c.name, arguments=dict(c.arguments)) for i, c in enumerate(turn.tool_calls)]
            self.replayed += 1
            self.log.append({"tag": "investigate", "turn": n + 1, "source": "replay", "tools": [c.name for c in calls]})
            return ModelResponse(message=Message(role="assistant", content=turn.content, tool_calls=calls), usage=Usage(input_tokens=0, output_tokens=0), stop_reason="tool_use", model="prefix")
        if not self.switched:
            self._verify(messages)  # the switch point itself is verified before the live model sees anything
            self.switched = True
            self.log.append({"tag": "investigate", "turn": n + 1, "source": "switch", "verified_tool_results": self.verified_tool_results})
        return self.live.generate(messages, tool_schemas, config)


def prefix_run(*, artifact: Path, switch_at: int, model_spec: str, out_dir: Path, billing: Any) -> dict[str, Any]:
    from .trials import build_trial_model, run_trial

    art = json.loads(Path(artifact).read_text(encoding="utf-8"))
    task = EvalTask.from_dict(art["task"])
    if task.fault_plan:
        raise ValueError("prefix diagnostics do not support tasks with a fault plan")
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise FileExistsError(f"{out_dir} exists")
    out_dir.mkdir(parents=True)
    live = build_trial_model(task, model_spec)
    model = PrefixModel(snapshot=art["snapshot"], switch_at=switch_at, live=live)
    status = "completed"
    error = None
    result = None
    try:
        result = run_trial(task, "dynamic", model_spec=model_spec, billing=billing, workdir=out_dir / "dbs", model=model, versions={"prefix_of": str(artifact), "switch_at": switch_at})
        if result.failure_class == "unreproducible" or (result.error and "Unreproducible" in result.error):
            status = "unreproducible"
            error = next((e for e in result.task_errors if "Unreproducible" in e), result.error)
    except Unreproducible as exc:
        status, error = "unreproducible", str(exc)
    record = {
        "ran_at": utc_now(), "artifact": str(artifact), "task_id": task.task_id, "switch_at": switch_at, "candidate_model": model_spec,
        "recorded_group": art.get("group"), "prefix": {"replayed_turns": model.replayed, "verified_tool_results": model.verified_tool_results, "switched": model.switched, "log": model.log},
        "status": status, "error": error,
        "suffix": None if result is None else {"status": result.status, "final_state": result.final_state, "conclusion_type": result.conclusion_type, "cost_usd": result.cost_usd, "model_calls": result.model_calls, "reads": result.reads, "layers": result.layers, "safety": result.safety, "failure_class": result.failure_class, "error": result.error},
        "note": "prefix turns are replayed at zero cost; the suffix is a live run and is never merged into end-to-end results",
    }
    (out_dir / "prefix.json").write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return record
