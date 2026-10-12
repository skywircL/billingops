from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
from langgraph_production.faults import CrashHook, no_crash

from billingops.billing_client import BillingClient
from billingops.config import Settings
from billingops.llm.fake import ScriptedModel
from billingops.runtime import CaseRuntime
from billingops.testing import BillingServer, duplicate_world


@pytest.fixture(scope="session")
def billing_server() -> Iterator[BillingServer]:
    tmp = tempfile.mkdtemp(prefix="billingops-billing-")
    server = BillingServer(Path(tmp) / "billing.sqlite3").start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def billing(billing_server: BillingServer) -> Iterator[BillingClient]:
    client = BillingClient(billing_server.url, admin_token="demo-admin")
    client.load_world(duplicate_world())
    try:
        yield client
    finally:
        try:
            client.clear_faults()
        finally:
            client.close()


RuntimeFactory = Callable[..., CaseRuntime]


@pytest.fixture
def runtime_factory(tmp_path: Path, billing_server: BillingServer) -> Iterator[RuntimeFactory]:
    """Runtimes sharing one app database, like restarts of one service."""
    created: list[CaseRuntime] = []

    def factory(*, model: Any | None = None, mode: str = "dynamic", crash_hook: CrashHook = no_crash) -> CaseRuntime:
        settings = Settings(app_db=str(tmp_path / "app.sqlite3"), billing_url=billing_server.url, mode=mode, model="fake")
        rt = CaseRuntime(settings=settings, model=model or ScriptedModel(), crash_hook=crash_hook)
        created.append(rt)
        return rt

    yield factory
    for rt in created:
        rt.close()


@pytest.fixture
def runtime(runtime_factory: RuntimeFactory, billing: BillingClient) -> CaseRuntime:
    return runtime_factory()


DUP_TEXT = "I was charged twice for invoice INV-2026-0142 (49.00 yuan each). Please refund the duplicate."


def run_case(rt: CaseRuntime, text: str = DUP_TEXT, *, customer_id: str = "C-1001", mode: str | None = None) -> dict[str, Any]:
    case = rt.submit_case(customer_id=customer_id, created_by="cust-c1001", text=text, mode=mode)
    rt.drain()
    return rt.case_view(case["id"])


def approve(rt: CaseRuntime, case_id: str, *, approver: str = "ops-dana") -> dict[str, Any]:
    case = rt.store.get_case(case_id)
    rt.submit_decision(case_id, decision="approve", approver=approver, note="checked", proposal_version=case["proposal_version"], token=None)
    rt.drain()
    return rt.case_view(case_id)
