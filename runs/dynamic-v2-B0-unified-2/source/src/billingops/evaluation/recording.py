"""Record one real trajectory into a replay trace. Recording stops before the external write."""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..billing_client import BillingClient
from ..config import Settings
from ..llm.replay import RecordingModel, Trace
from ..runtime import CaseRuntime
from ..testing import BillingServer, ambiguous_world, duplicate_world
from .experiment import build_model
from .simulators import SimulatedUser


def record_trajectory(*, trace_path: Path, text: str, answer: str | None, mode: str, model_spec: str, world: str) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="billingops-record-"))
    server = BillingServer(tmp / "billing.sqlite3").start()
    try:
        billing = BillingClient(server.url, admin_token="demo-admin")
        worlds = {"duplicate": duplicate_world(), "duplicate_partial": duplicate_world(prior_refund_cents=1500), "ambiguous": ambiguous_world()}
        billing.load_world(worlds[world])
        trace = Trace(trace_path)
        rt = CaseRuntime(settings=Settings(app_db=str(tmp / "app.sqlite3"), billing_url=server.url, mode=mode, model=model_spec), model=RecordingModel(build_model(model_spec), trace), billing=billing)
        case = rt.submit_case(customer_id="C-1001", created_by="cust-c1001", text=text, mode=mode)
        rt.drain()
        current = rt.store.get_case(case["id"])
        if current["state"] == "awaiting_user":
            reply = answer or SimulatedUser({"invoice_id": "INV-2026-0142", "payment_id": "PAY-88213", "amount_cents": 4900}).answer(current["pending_question"] or "")
            rt.submit_message(case["id"], text=reply, actor="cust-c1001")
            rt.drain()
        view = rt.case_view(case["id"])
        # Stop here: awaiting_approval or closed. No approval, no write, no execution is recorded.
        rt.close()
        return {"trace": str(trace_path), "entries": len(trace), "state": view["case"]["state"], "conclusion": (view["case"]["conclusion"] or {}).get("type"), "mode": mode}
    finally:
        server.stop()
