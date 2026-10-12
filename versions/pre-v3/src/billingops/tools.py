"""Read-only investigation tools, the clarification tool, and the end tool.

Nothing in here writes. The refund is not a tool: the investigation model can
propose it through ``end_investigation`` and code decides whether the proposal
is executable. Customer identity is bound when the runner is built, from
server context; no tool takes a customer id as an argument.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .billing_client import BillingClient, BillingUnavailable, NotFound

ID_PATTERN = r"^[A-Z]{2,4}-[A-Za-z0-9-]{1,32}$"

CONCLUSION_TYPES: tuple[str, ...] = (
    "duplicate_charge_refund",
    "no_duplicate_found",
    "explain_refund_status",
    "explain_subscription",
    "escalate",
    "insufficient_information",
)

READ_TOOLS: tuple[str, ...] = (
    "get_invoice",
    "list_invoices",
    "list_period_invoices",
    "list_payments",
    "get_payment",
    "list_refunds",
    "list_customer_refunds",
    "get_subscription_history",
    "get_policy",
)
CLARIFY_TOOL = "ask_customer"
END_TOOL = "end_investigation"
EXTRACT_TOOL = "submit_fields"


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else sorted(properties),
        "additionalProperties": False,
    }


INVESTIGATION_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_invoice",
        "description": "Fetch one invoice of this customer by id (e.g. INV-2026-0142).",
        "input_schema": _obj({"invoice_id": {"type": "string", "pattern": ID_PATTERN}}),
    },
    {
        "name": "list_invoices",
        "description": "List this customer's invoices, newest first. Use when no invoice id is known.",
        "input_schema": _obj({}),
    },
    {
        "name": "list_period_invoices",
        "description": "Other invoices of this customer whose billing period overlaps the given invoice. Use to tell a duplicate payment apart from two separate invoices in the same period.",
        "input_schema": _obj({"invoice_id": {"type": "string", "pattern": ID_PATTERN}}),
    },
    {
        "name": "list_payments",
        "description": "All payments recorded against one invoice, oldest first, with status and settlement.",
        "input_schema": _obj({"invoice_id": {"type": "string", "pattern": ID_PATTERN}}),
    },
    {
        "name": "get_payment",
        "description": "Fetch one payment of this customer by id (e.g. PAY-88213). Returns the invoice it belongs to.",
        "input_schema": _obj({"payment_id": {"type": "string", "pattern": ID_PATTERN}}),
    },
    {
        "name": "list_refunds",
        "description": "Refunds already recorded against one payment.",
        "input_schema": _obj({"payment_id": {"type": "string", "pattern": ID_PATTERN}}),
    },
    {
        "name": "list_customer_refunds",
        "description": "Every refund recorded for this customer. Use when the customer asks about a refund without naming a payment.",
        "input_schema": _obj({}),
    },
    {
        "name": "get_subscription_history",
        "description": "This customer's subscriptions and every plan or price change, oldest first.",
        "input_schema": _obj({}),
    },
    {
        "name": "get_policy",
        "description": "A versioned billing policy. Names: pricing, refunds. Omit version for the current one.",
        "input_schema": _obj(
            {
                "name": {"type": "string", "enum": ["pricing", "refunds"]},
                "version": {"type": ["integer", "null"]},
            }
        ),
    },
    {
        "name": CLARIFY_TOOL,
        "description": "Ask the customer one specific question and pause until they answer. Costs one of a small number of clarifications; ask only for information you cannot look up.",
        "input_schema": _obj({"question": {"type": "string", "minLength": 5, "maxLength": 500}}),
    },
    {
        "name": END_TOOL,
        "description": (
            "Finish the investigation. For duplicate_charge_refund name the extra payment to refund; code will "
            "compute the amount and check eligibility. Use no_duplicate_found when the records show one payment per "
            "invoice, explain_refund_status for refund questions, explain_subscription for price questions, escalate "
            "when a human must decide, insufficient_information when the customer could not identify the charge."
        ),
        "input_schema": _obj(
            {
                "conclusion_type": {"type": "string", "enum": list(CONCLUSION_TYPES)},
                "target_invoice_id": {"type": ["string", "null"]},
                "target_payment_id": {"type": ["string", "null"]},
                "summary": {"type": "string", "maxLength": 2000},
                "facts": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            }
        ),
    },
]

EXTRACT_TOOL_SCHEMA: dict[str, Any] = {
    "name": EXTRACT_TOOL,
    "description": "Record the structured fields found in the customer's message. Copy identifiers exactly; do not invent any.",
    "input_schema": _obj(
        {
            "invoice_ids": {"type": "array", "items": {"type": "string"}},
            "payment_ids": {"type": "array", "items": {"type": "string"}},
            "amounts_cents": {"type": "array", "items": {"type": "integer"}},
            "mentions_duplicate": {"type": "boolean"},
            "mentions_refund_status": {"type": "boolean"},
            "mentions_subscription_price": {"type": "boolean"},
            "summary": {"type": "string", "maxLength": 400},
        }
    ),
}


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "data": self.data, "error": self.error}


@dataclass
class ToolRunner:
    """Executes read tools for one customer. Budget accounting lives in the graph."""

    billing: BillingClient
    customer_id: str
    unavailable_hook: Callable[[str], None] = field(default=lambda _tool: None)

    def run(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name not in READ_TOOLS:
            return ToolResult(False, error=f"{name} is not a read tool")
        try:
            data = self._dispatch(name, args)
        except NotFound as exc:
            return ToolResult(False, error=f"not_found: {exc}")
        except BillingUnavailable as exc:
            self.unavailable_hook(name)
            return ToolResult(False, error=f"billing_unavailable: {exc}")
        except (KeyError, TypeError, ValueError) as exc:
            return ToolResult(False, error=f"invalid_arguments: {exc}")
        return ToolResult(True, data=data)

    def _dispatch(self, name: str, args: dict[str, Any]) -> Any:
        c = self.customer_id
        if name == "get_invoice":
            return self.billing.get_invoice(c, str(args["invoice_id"]))
        if name == "list_invoices":
            return self.billing.list_invoices(c)
        if name == "list_period_invoices":
            return self.billing.period_invoices(c, str(args["invoice_id"]))
        if name == "list_payments":
            return self.billing.invoice_payments(c, str(args["invoice_id"]))
        if name == "get_payment":
            return self.billing.get_payment(c, str(args["payment_id"]))
        if name == "list_refunds":
            return self.billing.payment_refunds(c, str(args["payment_id"]))
        if name == "list_customer_refunds":
            return self.billing.customer_refunds(c)
        if name == "get_subscription_history":
            return self.billing.subscription_history(c)
        if name == "get_policy":
            version = args.get("version")
            return self.billing.get_policy(str(args["name"]), int(version) if version is not None else None)
        raise KeyError(name)
