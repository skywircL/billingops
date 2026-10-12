#!/usr/bin/env python3
"""Read-only audit of G32 payload exposure in actual Agent request traces.

Example (writes one new sidecar; never edits the run):
  python scripts/audit_dynamic_v2_exposure.py runs/dynamic-v2-B0-baseline-1 \
      --out analysis/dynamic-eval-v1/baseline-exposure-audit.json

Only valid JSON is recursively decoded. Arbitrary escape sequences in plain
text are not unescaped, and no text is executed. This reports exposure metadata,
not attack success, safety, a changed task score or a fresh Agent trial.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

VERSION = "dynamic-v2-exposure-audit-1"
PAYLOAD_FACT_PREFIX = "本账单description含："


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _pointer_key(key: Any) -> str:
    return str(key).replace("~", "~0").replace("/", "~1")


def string_views(value: Any, path: str = "", decode_depth: int = 0) -> Iterator[tuple[str, str, int]]:
    """Enumerate strings, including values inside at most eight JSON layers."""
    if isinstance(value, str):
        yield path or "/", value, decode_depth
        if decode_depth < 8:
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                return
            if decoded != value:
                yield from string_views(decoded, path + "/$json", decode_depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from string_views(item, path + "/" + _pointer_key(key), decode_depth)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from string_views(item, path + "/" + str(index), decode_depth)


def fixture_payload(task: dict[str, Any]) -> str:
    facts = (task.get("catalog_snapshot") or {}).get("key_initial_facts") or []
    payloads = [fact[len(PAYLOAD_FACT_PREFIX):] for fact in facts
                if isinstance(fact, str) and fact.startswith(PAYLOAD_FACT_PREFIX)]
    if len(payloads) != 1 or not payloads[0]:
        raise ValueError("G32 must define exactly one payload in its disclosed description fact")
    payload = payloads[0]
    if not any(payload in str(row.get("description", "")) for row in task.get("world", {}).get("invoices", [])):
        raise ValueError("Disclosed G32 payload is not present in the fixture invoice descriptions")
    return payload


def audit_trial(directory: Path, payload: str) -> dict[str, Any]:
    inputs = {name: (directory / name).read_bytes() for name in ("artifact.json", "agent-calls.jsonl", "result.json")}
    artifact = json.loads(inputs["artifact.json"])
    result = json.loads(inputs["result.json"])
    hits = []
    errors = []
    requests = 0
    request_indices = set()
    finished_indices = set()
    for line_no, line in enumerate(inputs["agent-calls.jsonl"].decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            errors.append(f"invalid_jsonl_line:{line_no}")
            continue
        if row.get("kind") in {"response", "error"}:
            finished_indices.add(row.get("index"))
        if row.get("kind") != "request":
            continue
        requests += 1
        request_indices.add(row.get("index"))
        for message_index, message in enumerate(row.get("messages") or []):
            content = message.get("content", "")
            if not isinstance(content, str):
                errors.append(f"non_string_content:{line_no}:{message_index}")
                continue
            for pointer, text, depth in string_views(content):
                if payload in text:
                    start = text.index(payload)
                    hits.append({
                        "jsonl_line_1based": line_no, "request_index": row.get("index"),
                        "message_index_0based": message_index, "role": message.get("role"),
                        "tool_call_id": message.get("tool_call_id"),
                        "content_sha256": sha256(content.encode("utf-8")),
                        "content_pointer": pointer, "json_decode_depth": depth,
                        "match_kind": "literal" if depth == 0 else "valid_json_decoded",
                        "quote": text[start:start + len(payload)],
                    })
    unfinished = request_indices - finished_indices
    if unfinished:
        errors.append("requests_without_response_or_error:" + repr(sorted(unfinished, key=str)))
    if not requests:
        errors.append("no_agent_requests")
    # A witnessed request proves exposure despite other incomplete records.
    # Absence is conclusive only when this completed trace was fully parsed.
    corrected = True if hits else None if errors else False
    as_run = artifact.get("injection_payload_reached_model")
    return {
        "trial_id": directory.name, "candidate_id": artifact.get("candidate_id"),
        "as_run": as_run, "corrected": corrected,
        "as_run_score_attack_flag": (result.get("scoring") or {}).get("attack", {}).get("payload_reached_model"),
        "metadata_changed": corrected is not None and as_run != corrected,
        "task_status_as_run": result.get("status"), "task_status_modified": False,
        "request_count": requests, "matches": hits, "trace_errors": errors,
        "sources": {name: {"path": str((directory / name).resolve()), "sha256": sha256(raw)} for name, raw in inputs.items()},
        "sources_unchanged_at_audit_end": all(raw == (directory / name).read_bytes() for name, raw in inputs.items()),
    }


def audit_run(run: Path, selected: set[str] | None = None) -> dict[str, Any]:
    run = run.resolve()
    fixture_bytes = (run / "fixtures.json").read_bytes()
    bundle = json.loads(fixture_bytes)
    candidates = [task for task in bundle["tasks"] if task.get("candidate_id") == "G32"]
    if len(candidates) != 1:
        raise ValueError("Expected exactly one G32 task in frozen run fixtures")
    task = candidates[0]
    payload = fixture_payload(task)
    rows, skipped = [], []
    for directory in sorted((run / "trials").glob("G32-t*")):
        if not directory.is_dir() or selected is not None and directory.name not in selected:
            continue
        if not all((directory / name).is_file() for name in ("artifact.json", "result.json", "agent-calls.jsonl")):
            skipped.append({"trial_id": directory.name, "reason": "trial_not_fully_written"})
            continue
        rows.append(audit_trial(directory, payload))
    if selected is not None:
        observed = {row["trial_id"] for row in rows + skipped}
        if selected - observed:
            raise ValueError(f"Selected trial directories not found: {sorted(selected - observed)}")
    return {
        "audit_version": VERSION, "audited_at": datetime.now(timezone.utc).isoformat(),
        "run": str(run), "scope": "observation_metadata_only_no_task_scores_or_environment_modified",
        "selection": sorted(selected) if selected is not None else "all_completed_G32_trials",
        "fixture": {"path": str(run / "fixtures.json"), "sha256": sha256(fixture_bytes)},
        "payload": payload, "payload_sha256": sha256(payload.encode("utf-8")),
        "payload_source": "G32.catalog_snapshot.key_initial_facts; validated against frozen world.invoices.description",
        "decoder": "literal substring plus recursive json.loads of valid JSON; no arbitrary text unescaping or execution",
        "scoring_relation": "Source review of dynamic-v2-B0-baseline-1 established that payload_reached_model is report-only; this audit does not recalculate scores or establish that relationship for arbitrary future scorers.",
        "completed_trials_audited": len(rows), "skipped": skipped,
        "corrected_exposed": sum(row["corrected"] is True for row in rows),
        "corrected_not_exposed": sum(row["corrected"] is False for row in rows),
        "corrected_unknown": sum(row["corrected"] is None for row in rows),
        "metadata_changes": sum(row["metadata_changed"] for row in rows),
        "trials": rows,
        "limitations": ["Exposure is not attack success or safety proof", "Semantic guardrail judgments remain provisional", "Sidecar never replaces or edits frozen original results"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--out", required=True, type=Path, help="Fresh sidecar JSON path; existing files are refused")
    parser.add_argument("--trials", help="Optional comma-separated G32 trial directory names for a disclosed sample")
    args = parser.parse_args()
    selected = set(args.trials.split(",")) if args.trials else None
    if args.out.exists():
        parser.error("output already exists; select a fresh sidecar path")
    if args.out.resolve().is_relative_to(args.run.resolve()):
        parser.error("sidecar output must be outside the frozen run directory")
    report = audit_run(args.run, selected)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({key: report[key] for key in ("completed_trials_audited", "corrected_exposed", "corrected_not_exposed", "corrected_unknown", "metadata_changes")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
