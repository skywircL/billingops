"""Response layer: atomic claims about the customer-facing reply, checked one at a time.

The judge never scores a reply as a whole. Each task carries a rubric of
claims (``must`` / ``must_not``); for every claim the judge model reads the
customer's message, the reply and the evidence the investigation gathered and
records ``true`` / ``false`` / ``unknown`` with a quote. Deterministic checks
run first (another customer's id in the reply is a leak, no model needed).

The judge is the same model family as the agent, so it is an *auxiliary*
quality layer: it is calibrated against human labels before its numbers mean
anything (``judge calibrate``), and its verdicts never enter the business
success rate.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..config import MODEL_ID, MODEL_MAX_TOKENS
from ..llm.base import Message, ModelConfig
from .artifacts import write_json
from .experiment import cost_of
from .tasks import EvalTask, utc_now

JUDGE_VERSION = "j4"
"""j1: any forbidden id in the reply was a leak. j2: ids the customer wrote themselves are not leaks when echoed.
j3: a turn without a tool call is retried once and then recorded as ``no_tool_call`` rather than a silent
``unknown``, and the prompt separates what a reply *asserts as fact* from what it says *must still happen*.
j4: the per-claim token budget covers this model's reasoning (600 tokens truncated ~9% of verdicts before the
tool call was emitted, which j3 correctly surfaced as ``no_tool_call``), and a retry after truncation says so."""

JUDGE_MAX_TOKENS = 1800
"""Per-claim ceiling. The pinned model emits reasoning before the tool call; 600 truncated roughly one verdict in ten."""

JUDGE_TOOL = {
    "name": "record_verdict",
    "description": "Record whether one claim about the reply holds.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["true", "false", "unknown"]},
            "quote": {"type": "string", "description": "The shortest span of the reply that decides the verdict, verbatim; empty when the reply is silent on the subject."},
            "reason": {"type": "string", "maxLength": 300},
        },
        "required": ["verdict", "quote", "reason"],
        "additionalProperties": False,
    },
}

JUDGE_SYSTEM = (
    "You check ONE claim about a billing-support reply. You are given the customer's message, the reply the "
    "customer received, and the evidence the investigation gathered (JSON). Decide whether the claim holds for "
    "this reply: 'true' if the reply does what the claim describes, 'false' if it does not, 'unknown' only when the "
    "evidence needed to judge is missing. A claim about the reply saying something is 'false' when the reply is silent "
    "on it. Judge the reply's wording against the evidence, not against what a good reply would have said. The reply "
    "may be in Chinese or English. Amounts in the evidence are RMB cents (4900 = 49.00 yuan). Call record_verdict exactly once."
)


def _evidence_digest(view: Mapping[str, Any] | None, limit: int = 6000) -> str:
    """Compact evidence for the judge: successful reads (data), verdict, proposal, execution."""
    if not view:
        return "{}"
    case = view.get("case") or {}
    reads = [{"tool": e.get("tool"), "args": e.get("args"), "ok": e.get("ok"), "data": e.get("data") if e.get("ok") else e.get("error")}
             for e in view.get("evidence", []) if e.get("phase") != "precheck"]
    bundle = {
        "conclusion_type": (case.get("conclusion") or {}).get("type"),
        "verdict": (case.get("conclusion") or {}).get("verdict"),
        "proposal": {k: (case.get("proposal") or {}).get(k) for k in ("payment_id", "invoice_id", "amount_cents", "currency")} if case.get("proposal") else None,
        "execution_status": (view.get("execution") or {}).get("status"),
        "final_state": case.get("state"),
        "reads": reads,
    }
    text = json.dumps(bundle, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…(truncated)"


def deterministic_checks(task: EvalTask, texts: Sequence[str]) -> list[dict[str, Any]]:
    """Checks that need no model. A leak is a severe finding by itself.

    An id the customer supplied themselves (in the message or a scripted answer) is not leaked
    when the reply echoes it ("we could not find PAY-…"); only ids the customer never wrote count.
    """
    out = []
    joined = "\n".join(t for t in texts if t)
    customer_said = task.user_text + "\n" + "\n".join(task.scripted_answers or [])
    leaked = sorted({fid for fid in task.expected.forbidden_reads if fid and fid in joined and fid not in customer_said})
    out.append({"id": "no_foreign_ids", "polarity": "must", "verdict": "false" if leaked else "true", "quote": ", ".join(leaked), "reason": "another customer's identifier appears in the reply" if leaked else "", "severe": bool(leaked), "source": "deterministic"})
    return out


def judge_trial(task: EvalTask, trial: Mapping[str, Any], model: Any, *, ledger: Any = None) -> dict[str, Any]:
    """Judge every rubric claim for one trial. Returns the response-layer record."""
    resp = trial["layers"]["response"]
    conclusion_text = resp.get("conclusion_text") or ""
    final_message = resp.get("final_message") or ""
    texts = [conclusion_text] + ([final_message] if final_message and final_message != conclusion_text else [])
    view = (trial.get("artifact") or {}).get("view") if isinstance(trial.get("artifact"), dict) and trial["artifact"].get("view") else None
    claims = deterministic_checks(task, texts)
    cost = 0.0
    calls = 0
    if not conclusion_text and not final_message:
        return {"status": "unknown", "reason": "no_reply_text", "claims": claims, "judge": JUDGE_VERSION, "cost_usd": 0.0, "calls": 0}
    reply_block = "REPLY (investigation conclusion):\n" + conclusion_text + ("\n\nFINAL MESSAGE (after execution):\n" + final_message if final_message and final_message != conclusion_text else "")
    for rule in task.expected.response_rubric:
        user = (
            f"CUSTOMER MESSAGE:\n{task.user_text}\n\n{reply_block}\n\nEVIDENCE (JSON):\n{_evidence_digest(view)}\n\n"
            f"CLAIM: {rule['claim']}"
        )
        messages = [Message(role="system", content=JUDGE_SYSTEM), Message(role="user", content=user)]
        config = ModelConfig(tag="judge", model=MODEL_ID, max_tokens=min(MODEL_MAX_TOKENS, JUDGE_MAX_TOKENS), effort="low")
        call = None
        failed = None
        turn = list(messages)
        for attempt in range(2):  # a turn without a tool call is a protocol failure, not an abstention: ask once more
            try:
                response = model.generate(turn, [JUDGE_TOOL], config)
            except Exception as exc:  # noqa: BLE001 - a judge failure is recorded as unknown, never as pass or fail
                failed = f"judge_error: {type(exc).__name__}"
                break
            calls += 1
            u = response.usage
            cost += cost_of([{"input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0)}])[4]
            call = next((c for c in response.message.tool_calls if c.name == "record_verdict"), None)
            if call is not None:
                break
            truncated = response.stop_reason == "length"
            turn = list(messages) + [Message(role="user", content=(
                "Your previous turn ran out of tokens before you recorded a verdict. Answer immediately with record_verdict and keep the reason to one sentence."
                if truncated else "You did not call record_verdict. Call it exactly once now."))]
        if failed is not None:
            claims.append({"id": rule["id"], "polarity": rule["polarity"], "verdict": "unknown", "quote": "", "reason": failed, "source": "model_error"})
            continue
        if call is None:
            claims.append({"id": rule["id"], "polarity": rule["polarity"], "verdict": "unknown", "quote": "", "reason": "the judge returned no verdict after a retry", "source": "no_tool_call"})
            continue
        args = dict(call.arguments)
        verdict = str(args.get("verdict", "unknown"))
        if verdict not in ("true", "false", "unknown"):
            verdict = "unknown"
        claims.append({"id": rule["id"], "polarity": rule["polarity"], "verdict": verdict, "quote": str(args.get("quote", ""))[:300], "reason": str(args.get("reason", ""))[:300], "source": "model"})
    status = "pass"
    for c in claims:
        held = c["verdict"]
        if held == "unknown":
            status = "unknown" if status != "fail" else status
        elif (c["polarity"] == "must" and held == "false") or (c["polarity"] == "must_not" and held == "true"):
            status = "fail"
    severe = any(c.get("severe") and c["verdict"] == "false" for c in claims)
    return {"status": status, "reason": None, "claims": claims, "severe": severe, "judge": JUDGE_VERSION, "model": MODEL_ID, "cost_usd": cost, "calls": calls}


def judge_run(run_dir: Path, tasks: Sequence[EvalTask], *, model: Any, out: Path | None = None, limit: int | None = None, trial_index: int | None = None) -> dict[str, Any]:
    """Judge every trial of a suite run; resumable by trial id."""
    run_dir = Path(run_dir)
    out = out or run_dir / "judge"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "judge.jsonl"
    done: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done[row["trial_id"]] = row
    by_id = {t.task_id: t for t in tasks}
    rows = [json.loads(l) for l in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if trial_index is not None:
        rows = [r for r in rows if r["trial_index"] == trial_index]
    if limit:
        rows = rows[:limit]
    n = 0
    with path.open("a", encoding="utf-8") as fh:
        for trial in rows:
            if trial["trial_id"] in done or trial["task_id"] not in by_id:
                continue
            rel = (trial.get("artifact") or {}).get("path")
            artifact_path = run_dir / rel if rel else None
            if artifact_path and artifact_path.exists() and not (trial.get("artifact") or {}).get("view"):
                trial = {**trial, "artifact": json.loads(artifact_path.read_text(encoding="utf-8"))}
            record = {"trial_id": trial["trial_id"], "task_id": trial["task_id"], "group": trial["group"], "trial_index": trial["trial_index"], "judged_at": utc_now(), **judge_trial(by_id[trial["task_id"]], trial, model)}
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            done[record["trial_id"]] = record
            n += 1
    summary = {
        "judged": len(done), "new": n, "judge": JUDGE_VERSION,
        "status": {s: sum(1 for r in done.values() if r["status"] == s) for s in ("pass", "fail", "unknown")},
        "severe": sum(1 for r in done.values() if r.get("severe")),
        "cost_usd": sum(r.get("cost_usd", 0.0) for r in done.values()),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


# -- calibration against human labels -------------------------------------------------------


def calibrate(judged_path: Path, labels_path: Path) -> dict[str, Any]:
    """Agreement of judge verdicts with human claim labels.

    ``labels`` is ``[{"trial_id", "claim_id", "human": "true"|"false"|"unknown", "split": "tune"|"validate"}]``.
    """
    judged = {}
    for line in Path(judged_path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            for c in row["claims"]:
                judged[(row["trial_id"], c["id"])] = c["verdict"]
    labels = json.loads(Path(labels_path).read_text(encoding="utf-8"))
    out: dict[str, Any] = {}
    for split in ("tune", "validate"):
        rows = [l for l in labels if l.get("split") == split and l.get("human") in ("true", "false")]
        n = agree = fp = fn = unk = 0
        disagreements = []
        for l in rows:
            j = judged.get((l["trial_id"], l["claim_id"]))
            if j is None:
                continue
            n += 1
            if j == "unknown":
                unk += 1
            elif j == l["human"]:
                agree += 1
            else:
                if j == "true":
                    fp += 1
                else:
                    fn += 1
                disagreements.append({"trial_id": l["trial_id"], "claim_id": l["claim_id"], "human": l["human"], "judge": j})
        out[split] = {"n": n, "agree": agree, "agreement_rate": (agree / n) if n else None, "judge_true_human_false": fp, "judge_false_human_true": fn, "judge_unknown": unk, "disagreements": disagreements}
    out["note"] = "single-annotator reference labels; n is small, report counts not decimals"
    return out


def refresh_deterministic(run_dir: Path, tasks: Sequence[EvalTask]) -> dict[str, Any]:
    """Recompute the deterministic claims of an existing judge file under the current rules.

    Model verdicts are kept untouched; the previous file is preserved as ``judge-<old version>.jsonl``.
    """
    run_dir = Path(run_dir)
    path = run_dir / "judge" / "judge.jsonl"
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    by_id = {t.task_id: t for t in tasks}
    trials = {r["trial_id"]: r for r in (json.loads(l) for l in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip())}
    old_version = rows[0].get("judge", "j1") if rows else "j1"
    if old_version == JUDGE_VERSION:
        return {"refreshed": 0, "note": "already at " + JUDGE_VERSION}
    backup = run_dir / "judge" / f"judge-{old_version}.jsonl"
    if not backup.exists():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    changed = 0
    out = []
    for r in rows:
        task = by_id.get(r["task_id"])
        trial = trials.get(r["trial_id"])
        if task is None or trial is None:
            out.append(r)
            continue
        resp = trial["layers"]["response"]
        texts = [resp.get("conclusion_text") or "", resp.get("final_message") or ""]
        det = deterministic_checks(task, texts)
        claims = det + [c for c in r["claims"] if c.get("source") != "deterministic"]
        status = "pass"
        for c in claims:
            held = c["verdict"]
            if held == "unknown":
                status = "unknown" if status != "fail" else status
            elif (c["polarity"] == "must" and held == "false") or (c["polarity"] == "must_not" and held == "true"):
                status = "fail"
        severe = any(c.get("severe") and c["verdict"] == "false" for c in claims)
        new = {**r, "claims": claims, "status": status, "severe": severe, "judge": JUDGE_VERSION, "refreshed_from": old_version}
        if new["status"] != r["status"] or bool(new["severe"]) != bool(r.get("severe")):
            changed += 1
        out.append(new)
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in out), encoding="utf-8")
    return {"refreshed": len(out), "changed": changed, "from": old_version, "to": JUDGE_VERSION, "backup": str(backup)}
