"""Explicit live Langfuse read-back acceptance, using synthetic data and no paid LLM."""
from __future__ import annotations

import time
from pathlib import Path

from ..billing_client import BillingClient
from ..config import Settings
from ..evaluation.artifacts import utc_now, write_json
from ..llm.fake import ScriptedModel
from ..runtime import CaseRuntime
from ..testing import BillingServer, ambiguous_world, duplicate_world
from .tracing import Tracer, redact


def run_acceptance(out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=False)
    tracer = Tracer()
    if not tracer.enabled or not tracer._client.auth_check():
        write_json(out / "acceptance.json", {"status": "blocked", "reason": "Langfuse authentication/configuration unavailable"})
        return out
    server = BillingServer(out / "billing.sqlite3").start()
    billing = BillingClient(server.url, admin_token="demo-admin")
    runs, cases = [], []
    try:
        for scenario in ("normal", "clarify_resume"):
            billing.load_world(duplicate_world() if scenario == "normal" else ambiguous_world())
            settings = Settings(app_db=str(out / f"{scenario}.sqlite3"), billing_url=server.url, model="fake", mode="dynamic")
            rt = CaseRuntime(settings=settings, model=ScriptedModel(), billing=billing, tracer=tracer)
            try:
                text = "I was charged twice for invoice INV-2026-0142 (49.00 yuan each). Please refund the duplicate." if scenario == "normal" else "I was charged twice, please refund one."
                case = rt.submit_case(customer_id="C-1001", created_by="cust-c1001", text=text)
                cid = case["id"]
                rt.drain()
            finally:
                rt.close()
            # Recreate the runtime after waiting. Parent correlation must survive process-local state loss.
            rt = CaseRuntime(settings=settings, model=ScriptedModel(), billing=billing, tracer=tracer)
            try:
                view = rt.case_view(cid)
                if scenario == "clarify_resume":
                    if view["case"]["state"] != "awaiting_user":
                        raise AssertionError("clarification fixture did not pause")
                    rt.submit_message(cid, text="Invoice INV-2026-0142.", actor="cust-c1001")
                    rt.drain()
                    view = rt.case_view(cid)
                if view["case"]["state"] != "awaiting_approval":
                    raise AssertionError("expected approval pause")
                rt.submit_decision(cid, decision="approve", approver="ops-dana", note="synthetic telemetry acceptance", proposal_version=view["case"]["proposal_version"], token=None)
                rt.drain()
                view = rt.case_view(cid)
                assert view["case"]["state"] == "closed" and view["execution"]["status"] == "done"
                events = rt.store.events(cid)
                run_rows = [e["payload"] for e in events if e["kind"] == "observation_started"]
                assert all(r["parent_run_id"] == run_rows[i-1]["run_id"] for i, r in enumerate(run_rows) if i)
                runs.extend(run_rows)
                cases.append({"scenario": scenario, "case_id": cid, "final_state": view["case"]["state"], "run_count": len(run_rows)})
                write_json(out / f"{scenario}-events.json", redact(events))
            finally:
                rt.close()
    finally:
        billing.close()
        server.stop()
    tracer.flush()
    confirmed, pending = {}, {r["trace_id"]: r for r in runs}
    readback_errors = {}
    deadline = time.monotonic() + 50
    while pending and time.monotonic() < deadline:
        for trace_id, run in list(pending.items()):
            try:
                observations = tracer._client.api.observations.get_many(trace_id=trace_id, limit=100, fields="core,basic,metadata", request_options={"timeout_in_seconds": 10, "max_retries": 0}).data
                names = sorted(o.name for o in observations if o.name)
                roots = [o for o in observations if o.name == "billingops.task"]
                if not roots or roots[0].session_id != run["case_id"]:
                    continue
                metadata = roots[0].metadata or {}
                if any(str(metadata.get(k)) != str(run.get(k)) for k in ("task_id", "command_id", "parent_run_id")):
                    readback_errors[trace_id] = "correlation_metadata_mismatch"
                    continue
                confirmed[trace_id] = {**run, "session_id": roots[0].session_id, "observation_names": names, "delivery": "read_back_verified"}
                del pending[trace_id]
            except Exception as exc:
                readback_errors[trace_id] = type(exc).__name__
                continue
        if pending:
            time.sleep(2)
    names = {n for t in confirmed.values() for n in t["observation_names"]}
    required = {"model.extract", "model.investigate", "model.conclude", "approval_recorded", "refund_verified"}
    ok = not pending and required.issubset(names) and any(n.startswith("tool.") for n in names) and tracer.dropped == 0
    write_json(out / "acceptance.json", {"status": "passed" if ok else "not_verified", "checked_at": utc_now(), "model": "scripted stand-in; live telemetry transport", "cases": cases,
                                         "confirmed_traces": list(confirmed.values()), "pending_trace_ids": list(pending), "readback_errors": readback_errors, "missing_observations": sorted(required - names), "dropped_operations": tracer.dropped})
    links = '\n'.join(f'- [{r["case_id"]} / task {r["task_id"]}]({r.get("trace_url")})' for r in confirmed.values())
    (out / "report.md").write_text(f'# Langfuse 正向验收\n\n状态：{"通过" if ok else "尚未确认"}。使用合成业务与模拟模型；实际上传并通过服务端 API 读回确认，不产生模型费用。\n\n{links}\n\n详情见 acceptance.json。\n')
    return out
