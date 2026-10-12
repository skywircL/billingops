"""Fixed decisions. Changing anything here is a versioned change (see docs/DECISIONS.md)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | Path | None = None) -> dict[str, str]:
    """Read ``.env`` from the project root into ``os.environ`` without overriding existing values.

    Kept dependency-free on purpose. Values are never logged.
    """
    candidate = Path(path) if path else Path(__file__).resolve().parents[2] / ".env"
    loaded: dict[str, str] = {}
    if not candidate.exists():
        return loaded
    for raw in candidate.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


load_dotenv()

# -- model pin (D1) ---------------------------------------------------------
# One available model with native tool calling: DeepSeek V4.1 Flash, served as
# ``deepseek-flash`` on DeepSeek's OpenAI-compatible endpoint, fixed in .env.
# BILLINGOPS_MODEL selects the adapter: ``openai`` (the pinned model) or
# ``fake`` (the scripted stand-in used by the test suite).
MODEL_SDK = os.environ.get("BILLINGOPS_MODEL", "fake")
MODEL_ID = os.environ.get("BILLINGOPS_LLM_MODEL", "deepseek-flash")
MODEL_BASE_URL = os.environ.get("BILLINGOPS_LLM_BASE_URL", "https://api.deepseek.com/v1")
MODEL_MAX_TOKENS = int(os.environ.get("BILLINGOPS_LLM_MAX_TOKENS", "4096"))
MODEL_TEMPERATURE = 0.0
MODEL_EFFORT_INVESTIGATE = "medium"
MODEL_EFFORT_CONCLUDE = "medium"
MODEL_EFFORT_EXTRACT = "low"
"""Kept on the ModelConfig for adapters that expose an effort knob; the OpenAI-compatible adapter ignores it."""

# USD per million tokens, from the provider's published table on the pin date.
# Used only to report cost; the experiment also stores raw token counts.
MODEL_PRICE_INPUT_PER_M = float(os.environ.get("BILLINGOPS_PRICE_INPUT_PER_M", "0.30"))
MODEL_PRICE_OUTPUT_PER_M = float(os.environ.get("BILLINGOPS_PRICE_OUTPUT_PER_M", "1.20"))
MODEL_PRICE_CACHE_READ_PER_M = float(os.environ.get("BILLINGOPS_PRICE_CACHE_READ_PER_M", "0.006"))
MODEL_PRICE_CACHE_WRITE_PER_M = float(os.environ.get("BILLINGOPS_PRICE_CACHE_WRITE_PER_M", "0.30"))

# -- budgets per case, cumulative across pauses and restarts -----------------
MAX_READS = 8
MAX_CLARIFICATIONS = 2
MAX_INVESTIGATION_CALLS = 14
MAX_REPAIRS = 2

# -- execution ------------------------------------------------------------
MAX_AUTO_VERIFICATIONS = 3
BILLING_WRITE_TIMEOUT_S = 5.0
BILLING_READ_TIMEOUT_S = 5.0

CURRENCY = "CNY"
"""Amounts are integer RMB cents. No conversion, no taxes."""


@dataclass(frozen=True)
class Identity:
    """A server-side demo identity. Role claims in request bodies are ignored."""

    name: str
    role: str  # "customer" | "operator"
    customer_id: str | None = None


DEFAULT_IDENTITIES: dict[str, Identity] = {
    "cust-c1001": Identity(name="cust-c1001", role="customer", customer_id="C-1001"),
    "cust-c1002": Identity(name="cust-c1002", role="customer", customer_id="C-1002"),
    "cust-c1003": Identity(name="cust-c1003", role="customer", customer_id="C-1003"),
    "ops-dana": Identity(name="ops-dana", role="operator"),
    "ops-lee": Identity(name="ops-lee", role="operator"),
}


@dataclass
class Settings:
    app_db: str = field(default_factory=lambda: os.environ.get("BILLINGOPS_DB", "data/app.sqlite3"))
    billing_url: str = field(default_factory=lambda: os.environ.get("BILLING_URL", "http://127.0.0.1:8010"))
    billing_admin_token: str = field(default_factory=lambda: os.environ.get("BILLING_ADMIN_TOKEN", "demo-admin"))
    mode: str = field(default_factory=lambda: os.environ.get("BILLINGOPS_MODE", "dynamic"))
    """Default investigation mode for new cases: ``fixed`` (A) or ``dynamic`` (B)."""
    model: str = field(default_factory=lambda: os.environ.get("BILLINGOPS_MODEL", "fake"))
    """``fake`` for the scripted stand-in, ``openai`` for the pinned model (DeepSeek V4.1 Flash,
    configured in .env), ``replay:<path>`` to serve a recorded trajectory."""
    identities: dict[str, Identity] = field(default_factory=lambda: dict(DEFAULT_IDENTITIES))
