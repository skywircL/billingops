"""Run the two groups over a case set, one fresh database per (case, group).

Group A is the fixed workflow (``mode=fixed``), group B the dynamic
investigation (``mode=dynamic``). Both share extraction, conclusion
generation, tools, budgets, amount code, approval logic and execution. Local
response reuse and replay are not available here: the runner takes only a
live model or the scripted stand-in, and it labels which one it used.
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..billing_client import BillingClient
from ..config import (
    MODEL_ID,
    MODEL_PRICE_CACHE_READ_PER_M,
    MODEL_PRICE_CACHE_WRITE_PER_M,
    MODEL_PRICE_INPUT_PER_M,
    MODEL_PRICE_OUTPUT_PER_M,
    Settings,
)
from ..llm.fake import ScriptedModel
from ..runtime import CaseRuntime
from ..observability.tracing import Tracer
from .data import CUSTOMER, Case
from .scorer import score_case
from .simulators import ApprovalSimulator, SimulatedUser
from .artifacts import capture_trial, digest, source_manifest, write_json
from .rescore import grade

GROUP_MODE = {"A": "fixed", "B": "dynamic"}
INFRA_MARKERS = ("BillingUnavailable", "billing_unavailable", "RateLimit", "429", "overloaded", "APIConnection", "timeout", "Timeout", "InternalServer", "5xx")


@dataclass
class CaseResult:
    case_id: str
    family: str
    category: str
    group: str
    model: str
    success: bool | None
    checks: dict[str, bool]
    final_state: str
    conclusion_type: str | None
    proposal_amount_cents: int | None
    model_calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    reads: int
    clarifications: int
    repairs: int
    latency_s: float
    human_wait_s: float
    budget_exhausted: bool
    blocked_proposals: int
    gate_rejections: int
    runtime_failure: str | None
    policy_violation: str | None
    error: str | None = None
    trail: list[str] = field(default_factory=list)
    score_status: str = "fail"
    score_reason: str | None = None
    artifact: dict[str, Any] = field(default_factory=dict)


def cost_of(usage: list[dict[str, Any]]) -> tuple[int, int, int, int, float]:
    inp = sum(int(u.get("input_tokens", 0)) for u in usage)
    out = sum(int(u.get("output_tokens", 0)) for u in usage)
    cr = sum(int(u.get("cache_read_input_tokens", 0)) for u in usage)
    cw = sum(int(u.get("cache_creation_input_tokens", 0)) for u in usage)
    cost = (inp * MODEL_PRICE_INPUT_PER_M + out * MODEL_PRICE_OUTPUT_PER_M + cr * MODEL_PRICE_CACHE_READ_PER_M + cw * MODEL_PRICE_CACHE_WRITE_PER_M) / 1_000_000
    return inp, out, cr, cw, cost


def build_model(spec: str) -> Any:
    if spec == "fake":
        return ScriptedModel()
    if spec == "openai":
        from ..llm.openai_compat import OpenAICompatibleModel

        return OpenAICompatibleModel()
    raise ValueError(f"the experiment runs only with a live model or the stand-in, not {spec!r}")


def run_case(case: Case, group: str, *, model_spec: str, billing: BillingClient, workdir: Path, max_rounds: int = 12) -> CaseResult:
    mode = GROUP_MODE[group]
    db = workdir / f"{case.case_id}-{group}.sqlite3"
    if any(workdir.glob(f"{case.case_id}-{group}.sqlite3*")):
        raise FileExistsError(f"refusing to overwrite existing trial: {db}")
    billing.load_world(case.world)  # an independent copy of the same source snapshot
    settings = Settings(app_db=str(db), billing_url=billing.base_url, billing_admin_token=billing.admin_token or "", mode=mode, model=model_spec)
    rt = CaseRuntime(settings=settings, model=build_model(model_spec), billing=billing, tracer=Tracer() if model_spec == "openai" else Tracer(enabled=False))
    user = SimulatedUser(case.disclosable)
    approver = ApprovalSimulator()
    started = time.monotonic()
    error: str | None = None
    try:
        row = rt.submit_case(customer_id=CUSTOMER, created_by="cust-c1001", text=case.user_text, mode=mode)
        case_id = row["id"]
        for _ in range(max_rounds):
            rt.drain()
            current = rt.store.get_case(case_id)
            if current["state"] == "awaiting_user":
                rt.submit_message(case_id, text=user.answer(current["pending_question"] or ""), actor="cust-c1001")
            elif current["state"] == "awaiting_approval":
                rt.submit_decision(case_id, decision=approver.decide(current["proposal"]), approver=approver.approver, note="simulated approval", proposal_version=current["proposal_version"], token=None)
            else:
                break
        view = rt.case_view(case_id)
    except Exception:
        error = traceback.format_exc()
        view = rt.case_view(case_id) if "case_id" in locals() else None
    latency = time.monotonic() - started

    # Database end state: the named payment when the ground truth names one, else every refund of the customer.
    target = case.ground_truth.get("db_check_payment_id") or case.ground_truth.get("expected_target_payment_id") or ((view or {}).get("case", {}).get("proposal") or {}).get("payment_id")
    refunds = None
    try:
        refunds = billing.payment_refunds(CUSTOMER, target) if target else billing.customer_refunds(CUSTOMER)
    except Exception:
        pass  # A grader read failure must not abort the experiment or invent an empty database.
    artifact = capture_trial(case=case, group=group, runtime=rt, view=view, billing=billing, result_dir=workdir.parent)
    if view is None:
        rt.close()
        return CaseResult(case.case_id, case.family, case.category, group, model_spec, False, {}, "error", None, None, 0, 0, 0, 0, 0, 0.0, 0, 0, 0, latency, 0.0, False, 0, 0, "own", None, error, artifact=artifact)

    score = grade(case.ground_truth, view, refunds)
    usage = view.get("usage", [])
    inp, out, cr, cw, cost = cost_of(usage)
    budgets = view.get("budgets") or {}
    events = rt.store.events(view["case"]["id"])
    kinds = [e["kind"] for e in events]
    state = view["case"]["state"]
    task_errors = " ".join((t.get("error") or "") for t in rt.store.tasks(view["case"]["id"]))
    runtime_failure = None
    if state == "error" or error:
        runtime_failure = "infra" if any(m in (task_errors + (view["case"].get("error") or "") + (error or "")) for m in INFRA_MARKERS) else "own"
    elif "budget_exhausted" in kinds and state == "escalated":
        runtime_failure = "own"
    violation = None
    approved = "approval_recorded" in kinds
    if refunds and not approved and case.ground_truth.get("expected_refund_total_cents", 0) == 0:
        violation = "refund_without_approval"
    proposal = view["case"].get("proposal") or {}
    if proposal and view.get("execution") and view["execution"].get("status") == "done":
        receipt = view["execution"].get("receipt") or {}
        if int(receipt.get("amount_cents", -1)) != int(proposal.get("amount_cents", -2)):
            violation = "executed_amount_differs_from_proposal"
    result = CaseResult(
        case_id=case.case_id,
        family=case.family,
        category=case.category,
        group=group,
        model=MODEL_ID if model_spec == "openai" else model_spec,
        success=False if runtime_failure or error else None if score["status"] == "unscorable" else score["status"] == "pass",
        checks={k: v["value"] for k, v in score["checks"].items()},
        final_state=state,
        conclusion_type=(view["case"].get("conclusion") or {}).get("type"),
        proposal_amount_cents=proposal.get("amount_cents"),
        model_calls=len(usage),
        input_tokens=inp,
        output_tokens=out,
        cache_read_tokens=cr,
        cache_write_tokens=cw,
        cost_usd=cost,
        reads=int(budgets.get("reads", 0)),
        clarifications=int(budgets.get("clarifications", 0)),
        repairs=int(budgets.get("repairs", 0)),
        latency_s=latency,
        human_wait_s=float(view["case"].get("human_wait_seconds", 0)),
        budget_exhausted="budget_exhausted" in kinds,
        blocked_proposals=len(view.get("blocked_proposals", [])),
        gate_rejections=kinds.count("gate_rejected"),
        runtime_failure=runtime_failure,
        policy_violation=violation,
        error=error,
        trail=view.get("trail", []),
        score_status="fail" if runtime_failure or error else score["status"],
        score_reason="runtime_failure" if runtime_failure or error else score.get("reason"),
        artifact=artifact,
    )
    rt.close()
    return result


def run_experiment(cases: list[Case], *, groups: tuple[str, ...], model_spec: str, billing: BillingClient, out_dir: Path, label: str) -> Path:
    if any((out_dir / p).exists() for p in ("manifest.json", "results.jsonl", "dbs", "artifacts")):
        raise FileExistsError("choose a fresh experiment directory; existing runs are immutable")
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    manifest = {
        "label": label,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL_ID if model_spec == "openai" else model_spec,
        "model_spec": model_spec,
        "groups": list(groups),
        "cases": len(cases),
        "runs": len(cases) * len(groups),
        "families": sorted({c.family for c in cases}),
        "note": "scripted stand-in: harness validation only, not evidence about any model" if model_spec == "fake" else "live model, no local reuse or replay",
        "provenance": source_manifest(),
        "task_set_sha256": digest([c.to_dict() for c in cases]),
        "policies_sha256": digest([c.world.get("policies", []) for c in cases]),
        "execution_scope": "CaseRuntime with real Billing HTTP; API ingress and queue latency are not measured",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    write_json(out_dir / "tasks.json", [c.to_dict() for c in cases])
    workdir = out_dir / "dbs"
    workdir.mkdir(exist_ok=True)
    with results_path.open("w", encoding="utf-8") as fh:
        for n, case in enumerate(cases, 1):
            for group in groups:
                result = run_case(case, group, model_spec=model_spec, billing=billing, workdir=workdir)
                fh.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
                fh.flush()
                mark = "ok " if result.success else "FAIL"
                print(f"[{n:3d}/{len(cases)}] {group} {mark} {case.case_id:32s} {result.final_state:18s} {result.conclusion_type or '-':24s} calls={result.model_calls} reads={result.reads}", flush=True)
    return results_path
