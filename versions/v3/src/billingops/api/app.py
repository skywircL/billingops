"""Persistent asynchronous case API.

Every command-producing POST persists a command and a task, then answers 202.
The executor picks the task up; the caller polls ``GET /cases/{id}``.

Idempotency: each such POST carries a client ``Idempotency-Key``. Keys are
scoped by caller, endpoint and case; the server keeps a digest of the body.
Same key and same body returns the original answer; same key with a different
body is a 409. A new operation needs a new key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from langgraph_production.models import digest
from pydantic import BaseModel, ConfigDict, Field

from ..config import Identity, Settings
from ..runtime import CaseRuntime, StateConflict

WEB_DIR = Path(__file__).resolve().parents[3] / "web"


class SubmitCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=3, max_length=4000)
    customer_id: str | None = None
    mode: str | None = Field(default=None, pattern=r"^(fixed|dynamic)$")


class SubmitMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)


class SubmitDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern=r"^(approve|reject)$")
    proposal_version: int
    note: str = ""
    token: str | None = None


class SubmitEscalation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=1000)


def create_app(runtime: CaseRuntime, *, start_executor: bool = True) -> FastAPI:
    settings: Settings = runtime.settings
    app = FastAPI(title="BillingOps Agent", version="0.1.0")
    app.state.runtime = runtime

    @app.on_event("startup")
    def _startup() -> None:
        if start_executor:
            runtime.start_executor()

    @app.on_event("shutdown")
    def _shutdown() -> None:
        runtime.stop_executor()

    # -- identity ---------------------------------------------------------------------

    def identity(x_demo_identity: str | None = Header(default=None)) -> Identity:
        ident = settings.identities.get(x_demo_identity or "")
        if ident is None:
            raise HTTPException(status_code=401, detail="unknown demo identity; set X-Demo-Identity")
        return ident

    def operator(ident: Identity = Depends(identity)) -> Identity:
        if ident.role != "operator":
            raise HTTPException(status_code=403, detail="operator identity required")
        return ident

    def visible_case(case_id: str, ident: Identity) -> dict[str, Any]:
        case = runtime.store.get_case(case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="case not found")
        if ident.role != "operator" and case["customer_id"] != ident.customer_id:
            raise HTTPException(status_code=404, detail="case not found")
        return case

    # -- idempotency -------------------------------------------------------------------------

    def idempotent(request: Request, ident: Identity, case_id: str | None, body: dict[str, Any], perform: Callable[[], tuple[int, dict[str, Any], str | None]]) -> JSONResponse:
        key = request.headers.get("Idempotency-Key")
        if not key or len(key) > 200:
            raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
        scope_key = f"{ident.name}|{request.url.path}|{case_id or '-'}|{key}"
        body_digest = digest(body)
        existing = runtime.store.lookup_idempotency(scope_key)
        if existing is not None:
            if existing["digest"] != body_digest:
                raise HTTPException(status_code=409, detail="Idempotency-Key reused with a different request body")
            if existing["status_code"] == 0:
                raise HTTPException(status_code=409, detail="request with this Idempotency-Key is still in progress")
            return JSONResponse(status_code=existing["status_code"], content=existing["response"], headers={"X-Idempotent-Replay": "1"})
        if not runtime.store.store_idempotency(scope_key=scope_key, digest=body_digest, status_code=0, response={}, command_id=None):
            raise HTTPException(status_code=409, detail="concurrent request with this Idempotency-Key")
        try:
            status, response, command_id = perform()
        except Exception:
            _release(scope_key)
            raise
        _complete(scope_key, status, response, command_id)
        return JSONResponse(status_code=status, content=response)

    def _release(scope_key: str) -> None:
        with runtime.store._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM idempotency WHERE scope_key = ? AND status_code = 0", (scope_key,))
            c.execute("COMMIT")

    def _complete(scope_key: str, status: int, response: dict[str, Any], command_id: str | None) -> None:
        import json

        with runtime.store._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("UPDATE idempotency SET status_code = ?, response_json = ?, command_id = ? WHERE scope_key = ?", (status, json.dumps(response, default=str), command_id, scope_key))
            c.execute("COMMIT")

    def conflict_to_http(fn: Callable[[], tuple[int, dict[str, Any], str | None]]) -> Callable[[], tuple[int, dict[str, Any], str | None]]:
        def wrapped() -> tuple[int, dict[str, Any], str | None]:
            try:
                return fn()
            except StateConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except LookupError as exc:
                raise HTTPException(status_code=404, detail="case not found") from exc

        return wrapped

    # -- endpoints -------------------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "mode": settings.mode, "model": settings.model}

    @app.post("/cases", status_code=202)
    def post_case(body: SubmitCase, request: Request, ident: Identity = Depends(identity)) -> JSONResponse:
        if ident.role == "operator":
            if not body.customer_id:
                raise HTTPException(status_code=422, detail="operators must name customer_id")
            customer_id = body.customer_id
        else:
            if body.customer_id and body.customer_id != ident.customer_id:
                raise HTTPException(status_code=403, detail="customers may only open cases for themselves")
            customer_id = str(ident.customer_id)

        def perform() -> tuple[int, dict[str, Any], str | None]:
            case = runtime.submit_case(customer_id=customer_id, created_by=ident.name, text=body.text, mode=body.mode)
            return 202, {"case_id": case["id"], "state": case["state"]}, None

        return idempotent(request, ident, None, body.model_dump(), perform)

    @app.post("/cases/{case_id}/messages", status_code=202)
    def post_message(case_id: str, body: SubmitMessage, request: Request, ident: Identity = Depends(identity)) -> JSONResponse:
        visible_case(case_id, ident)

        def perform() -> tuple[int, dict[str, Any], str | None]:
            command_id = runtime.submit_message(case_id, text=body.text, actor=ident.name)
            return 202, {"case_id": case_id, "command_id": command_id, "state": runtime.store.get_case(case_id)["state"]}, command_id

        return idempotent(request, ident, case_id, body.model_dump(), conflict_to_http(perform))

    @app.post("/cases/{case_id}/decision", status_code=202)
    def post_decision(case_id: str, body: SubmitDecision, request: Request, ident: Identity = Depends(operator)) -> JSONResponse:
        visible_case(case_id, ident)

        def perform() -> tuple[int, dict[str, Any], str | None]:
            command_id = runtime.submit_decision(case_id, decision=body.decision, approver=ident.name, note=body.note, proposal_version=body.proposal_version, token=body.token)
            return 202, {"case_id": case_id, "command_id": command_id, "state": runtime.store.get_case(case_id)["state"]}, command_id

        return idempotent(request, ident, case_id, body.model_dump(), conflict_to_http(perform))

    @app.post("/cases/{case_id}/resume", status_code=202)
    def post_resume(case_id: str, request: Request, ident: Identity = Depends(operator)) -> JSONResponse:
        visible_case(case_id, ident)

        def perform() -> tuple[int, dict[str, Any], str | None]:
            command_id = runtime.submit_resume(case_id, actor=ident.name)
            return 202, {"case_id": case_id, "command_id": command_id, "state": runtime.store.get_case(case_id)["state"]}, command_id

        return idempotent(request, ident, case_id, {}, conflict_to_http(perform))

    @app.post("/cases/{case_id}/escalate", status_code=202)
    def post_escalate(case_id: str, body: SubmitEscalation, request: Request, ident: Identity = Depends(operator)) -> JSONResponse:
        visible_case(case_id, ident)

        def perform() -> tuple[int, dict[str, Any], str | None]:
            command_id = runtime.submit_escalation(case_id, operator=ident.name, reason=body.reason)
            return 202, {"case_id": case_id, "command_id": command_id, "state": runtime.store.get_case(case_id)["state"]}, command_id

        return idempotent(request, ident, case_id, body.model_dump(), conflict_to_http(perform))

    @app.get("/cases")
    def list_cases(ident: Identity = Depends(identity)) -> list[dict[str, Any]]:
        cases = runtime.store.list_cases()
        if ident.role != "operator":
            cases = [c for c in cases if c["customer_id"] == ident.customer_id]
        return [{k: c[k] for k in ("id", "customer_id", "mode", "state", "created_at", "updated_at", "proposal_version")} for c in cases]

    @app.get("/cases/{case_id}")
    def get_case(case_id: str, ident: Identity = Depends(identity)) -> dict[str, Any]:
        visible_case(case_id, ident)
        view = runtime.case_view(case_id)
        assert view is not None
        return view

    @app.get("/cases/{case_id}/events")
    def get_events(case_id: str, ident: Identity = Depends(identity)) -> list[dict[str, Any]]:
        visible_case(case_id, ident)
        return runtime.store.events(case_id)

    @app.get("/metrics/summary")
    def metrics(ident: Identity = Depends(identity)) -> dict[str, Any]:
        return runtime.store.metrics_summary()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    return app
