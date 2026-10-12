"""A LangGraph agent plus the reliability layer that makes it shippable.

The graph is the ordinary part: route, call a tool, stop for a human, reply.
What this package is actually about is everything wrapped around it, namely
durable checkpoints, side effects that survive a resume without repeating,
approvals that authorise one thing once, and an evaluation harness that turns
"it routes well" into a number a build can fail on.
"""

from .approvals import (
    ApprovalLedger,
    AuthorizationError,
    InMemoryApprovalLedger,
    SqliteApprovalLedger,
    authorize,
    mint_token,
)
from .effects import (
    EffectFailed,
    EffectInDoubt,
    EffectStore,
    InMemoryEffectStore,
    ReconciliationError,
    SqliteEffectStore,
    idempotency_key,
    open_effects,
    perform_effect,
    resolve_effect,
)
from .faults import CRASH_POINTS, CrashInjector, SimulatedCrash, no_crash
from .graph import GraphDeps, build_graph
from .llm import LLMCallable, LLMRequest, OfflineSupportLLM, from_chat_model
from .models import (
    ApprovalRecord,
    EffectReceipt,
    GateRequest,
    HumanDecision,
    PlannedAction,
    RouterDecision,
    Ticket,
    ToolCall,
)
from .router import parse_router_output
from .runtime import DriveOutcome, RunResult, SupportRuntime, drive
from .tools import ToolRegistry, ToolSpec

__version__ = "0.1.0"

__all__ = [
    "CRASH_POINTS",
    "ApprovalLedger",
    "ApprovalRecord",
    "AuthorizationError",
    "CrashInjector",
    "DriveOutcome",
    "EffectFailed",
    "EffectInDoubt",
    "EffectReceipt",
    "EffectStore",
    "GateRequest",
    "GraphDeps",
    "HumanDecision",
    "InMemoryApprovalLedger",
    "InMemoryEffectStore",
    "LLMCallable",
    "LLMRequest",
    "OfflineSupportLLM",
    "PlannedAction",
    "ReconciliationError",
    "RouterDecision",
    "RunResult",
    "SimulatedCrash",
    "SqliteApprovalLedger",
    "SqliteEffectStore",
    "SupportRuntime",
    "Ticket",
    "ToolCall",
    "ToolRegistry",
    "ToolSpec",
    "authorize",
    "build_graph",
    "drive",
    "from_chat_model",
    "idempotency_key",
    "mint_token",
    "no_crash",
    "open_effects",
    "parse_router_output",
    "perform_effect",
    "resolve_effect",
    "__version__",
]
