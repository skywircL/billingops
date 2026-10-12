"""The tool surface of the support desk.

Every tool declares whether it is irreversible. That single flag is what the
graph routes on: irreversible tools cannot reach their handler without passing
the human gate first, and no prompt wording can talk the graph out of it.

The handlers read a small synthetic dataset. There is no network here and
there never will be: this repo is about the control flow around tools, not
about any particular backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# --------------------------------------------------------------------------
# Argument schemas. The patterns are strict on purpose: a model that invents
# an identifier fails validation here instead of reaching a handler.
# --------------------------------------------------------------------------

ORDER_REF = r"^ORD-\d{5}$"
INVOICE_REF = r"^INV-\d{5}$"
ACCOUNT_REF = r"^ACC-\d{4}$"


class OrderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_ref: str = Field(pattern=ORDER_REF)


class InvoiceArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_ref: str = Field(pattern=INVOICE_REF)


class RegionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    region: str = Field(pattern=r"^(eu-west|us-east|ap-south)$")


class RefundArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_ref: str = Field(pattern=INVOICE_REF)
    amount_cents: int = Field(gt=0, le=1_000_000)


class AccountArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_ref: str = Field(pattern=ACCOUNT_REF)


# --------------------------------------------------------------------------
# Synthetic records. Fictional company, fictional customers, invented ids.
# --------------------------------------------------------------------------

ORDERS: Mapping[str, dict[str, Any]] = {
    "ORD-40021": {"status": "in_transit", "carrier": "Northline", "eta_days": 2},
    "ORD-40088": {"status": "held_at_hub", "carrier": "Northline", "eta_days": 5},
    "ORD-40155": {"status": "delivered", "carrier": "Vespera", "eta_days": 0},
    "ORD-40201": {"status": "delivered", "carrier": "Vespera", "eta_days": 0},
}

INVOICES: Mapping[str, dict[str, Any]] = {
    "INV-10032": {"total_cents": 9000, "currency": "USD", "period": "2026-05", "paid": True},
    "INV-10077": {"total_cents": 24000, "currency": "USD", "period": "2026-06", "paid": True},
    "INV-10120": {"total_cents": 1990, "currency": "USD", "period": "2026-06", "paid": True},
    "INV-10155": {"total_cents": 4500, "currency": "USD", "period": "2026-07", "paid": False},
    "INV-10188": {"total_cents": 30000, "currency": "USD", "period": "2026-07", "paid": True},
}

ACCOUNTS: Mapping[str, dict[str, Any]] = {
    "ACC-2041": {"plan": "pro", "seats": 12, "region": "eu-west"},
    "ACC-2077": {"plan": "enterprise", "seats": 140, "region": "us-east"},
    "ACC-2155": {"plan": "pro", "seats": 30, "region": "ap-south"},
}

REGION_STATUS: Mapping[str, dict[str, Any]] = {
    "eu-west": {"state": "degraded", "incident": "INC-908", "component": "query engine"},
    "us-east": {"state": "operational", "incident": None, "component": None},
    "ap-south": {"state": "maintenance", "incident": "INC-911", "component": "sync workers"},
}


class UnknownRecord(LookupError):
    """A well formed identifier that does not exist in the dataset."""


def _lookup_order(args: OrderArgs) -> dict[str, Any]:
    record = ORDERS.get(args.order_ref)
    if record is None:
        raise UnknownRecord(args.order_ref)
    return {"order_ref": args.order_ref, **record}


def _lookup_invoice(args: InvoiceArgs) -> dict[str, Any]:
    record = INVOICES.get(args.invoice_ref)
    if record is None:
        raise UnknownRecord(args.invoice_ref)
    return {"invoice_ref": args.invoice_ref, **record}


def _check_service_status(args: RegionArgs) -> dict[str, Any]:
    return {"region": args.region, **REGION_STATUS[args.region]}


def _issue_refund(args: RefundArgs) -> dict[str, Any]:
    invoice = INVOICES.get(args.invoice_ref)
    if invoice is None:
        raise UnknownRecord(args.invoice_ref)
    return {
        "invoice_ref": args.invoice_ref,
        "refunded_cents": args.amount_cents,
        "remaining_cents": max(invoice["total_cents"] - args.amount_cents, 0),
    }


def _cancel_subscription(args: AccountArgs) -> dict[str, Any]:
    account = ACCOUNTS.get(args.account_ref)
    if account is None:
        raise UnknownRecord(args.account_ref)
    return {"account_ref": args.account_ref, "previous_plan": account["plan"], "state": "cancelled"}


def _reset_credentials(args: AccountArgs) -> dict[str, Any]:
    account = ACCOUNTS.get(args.account_ref)
    if account is None:
        raise UnknownRecord(args.account_ref)
    return {"account_ref": args.account_ref, "seats_notified": account["seats"], "state": "reset_sent"}


@dataclass(frozen=True)
class ToolSpec:
    """One tool: its schema, its handler, and whether it can be undone."""

    name: str
    description: str
    irreversible: bool
    args_model: type[BaseModel]
    handler: Callable[[Any], dict[str, Any]]

    def validate_args(self, args: Mapping[str, Any]) -> BaseModel:
        return self.args_model.model_validate(dict(args))

    def run(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self.handler(self.validate_args(args))


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="lookup_order_status",
        description="Read the shipping status of a hardware order. Read only.",
        irreversible=False,
        args_model=OrderArgs,
        handler=_lookup_order,
    ),
    ToolSpec(
        name="lookup_invoice",
        description="Read one invoice and its line total. Read only.",
        irreversible=False,
        args_model=InvoiceArgs,
        handler=_lookup_invoice,
    ),
    ToolSpec(
        name="check_service_status",
        description="Read the current platform status for one region. Read only.",
        irreversible=False,
        args_model=RegionArgs,
        handler=_check_service_status,
    ),
    ToolSpec(
        name="issue_refund",
        description="Move money back to the customer. Cannot be undone.",
        irreversible=True,
        args_model=RefundArgs,
        handler=_issue_refund,
    ),
    ToolSpec(
        name="cancel_subscription",
        description="End a paying subscription. Cannot be undone.",
        irreversible=True,
        args_model=AccountArgs,
        handler=_cancel_subscription,
    ),
    ToolSpec(
        name="reset_credentials",
        description="Invalidate every session on an account. Disruptive and not reversible.",
        irreversible=True,
        args_model=AccountArgs,
        handler=_reset_credentials,
    ),
)


class ToolRegistry:
    """Lookup surface for the tools the router is allowed to name."""

    def __init__(self, specs: tuple[ToolSpec, ...] = TOOLS) -> None:
        self._specs: dict[str, ToolSpec] = {spec.name: spec for spec in specs}

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def require(self, name: str) -> ToolSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"unknown tool: {name}")
        return spec

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    @property
    def irreversible_names(self) -> tuple[str, ...]:
        return tuple(name for name, spec in self._specs.items() if spec.irreversible)

    def is_irreversible(self, name: str) -> bool:
        spec = self._specs.get(name)
        return bool(spec and spec.irreversible)

    def catalogue(self) -> str:
        """Human and model readable tool list, used inside the router prompt."""
        lines = []
        for spec in self._specs.values():
            fields = ", ".join(
                f"{field}: {info.annotation.__name__ if info.annotation else 'str'}"
                for field, info in spec.args_model.model_fields.items()
            )
            marker = " [IRREVERSIBLE, needs human approval]" if spec.irreversible else ""
            lines.append(f"- {spec.name}({fields}){marker}: {spec.description}")
        return "\n".join(lines)


def validation_errors(spec: ToolSpec, args: Mapping[str, Any]) -> str | None:
    """Return a compact error string, or None when the arguments are valid."""
    try:
        spec.validate_args(args)
    except ValidationError as exc:
        return "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors())
    return None
