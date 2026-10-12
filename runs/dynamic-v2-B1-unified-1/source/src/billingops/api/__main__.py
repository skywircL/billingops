"""``python -m billingops.api --db data/app.sqlite3 --billing-url http://127.0.0.1:8010``"""

from __future__ import annotations

import argparse
import os

import uvicorn

from ..config import Settings
from ..llm.fake import ScriptedModel
from ..llm.replay import ReplayModel, Trace
from ..runtime import CaseRuntime
from ..observability.tracing import Tracer
from .app import create_app


def build_model(spec: str):
    if spec == "fake":
        return ScriptedModel()
    if spec == "openai":
        from ..llm.openai_compat import OpenAICompatibleModel

        return OpenAICompatibleModel()
    if spec.startswith("replay:"):
        return ReplayModel(Trace.load(spec.split(":", 1)[1]))
    raise SystemExit(f"unknown model spec {spec!r}; use fake, openai, or replay:<path>")


def main() -> None:
    parser = argparse.ArgumentParser(description="BillingOps Agent API")
    parser.add_argument("--db", default=os.environ.get("BILLINGOPS_DB", "data/app.sqlite3"))
    parser.add_argument("--billing-url", default=os.environ.get("BILLING_URL", "http://127.0.0.1:8010"))
    parser.add_argument("--mode", default=os.environ.get("BILLINGOPS_MODE", "fixed"), choices=["fixed", "dynamic"])
    parser.add_argument("--model", default=os.environ.get("BILLINGOPS_MODEL", "fake"))
    parser.add_argument("--agent-revision", choices=["B0", "B1"], default="B0")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    settings = Settings(app_db=args.db, billing_url=args.billing_url, mode=args.mode, model=args.model, agent_revision=args.agent_revision)
    runtime = CaseRuntime(settings=settings, model=build_model(args.model), tracer=Tracer())
    try:
        uvicorn.run(create_app(runtime), host=args.host, port=args.port, log_level="info")
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
