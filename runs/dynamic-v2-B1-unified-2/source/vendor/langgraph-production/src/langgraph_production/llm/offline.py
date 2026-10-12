"""The offline stand-in used by the tests, the demo, and the eval harness.

This is not a language model. It is a deterministic keyword classifier that
speaks the same wire format a model is asked to speak, so the whole system can
run with no key, no network, and no variance.

Be clear about what that means for the numbers this repo reports: the routing
accuracy in the README measures *this* classifier, not any vendor's model. The
harness is the deliverable. Swap :class:`OfflineSupportLLM` for
``from_chat_model(...)`` and the same harness prints the same report for the
real thing.

The rules below are generic. None of them keys off a fixture id, and none of
them was written to rescue a specific labelled example. The mistakes the
report shows are real mistakes of a simple classifier.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .base import LLMRequest

ORDER_PATTERN = re.compile(r"ORD-\d{5}")
INVOICE_PATTERN = re.compile(r"INV-\d{5}")
ACCOUNT_PATTERN = re.compile(r"ACC-\d{4}")
REGION_PATTERN = re.compile(r"\b(eu-west|us-east|ap-south)\b")
MONEY_PATTERN = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")

# Anything in this set means the ticket carries legal, security, or commercial
# weight. No tool is the right answer for those.
ESCALATION_TERMS = (
    "lawyer",
    "legal action",
    "sue you",
    "gdpr",
    "data deletion",
    "personal data",
    "former employee",
    "breach",
    "saw our data",
    "another customer",
    "compliance",
    "renegotiate",
    "our contract",
    "discount",
    "compensation",
    "withhold payment",
    "regulator",
    "press",
)

# Security vocabulary gets its own group. A ticket that reads like an incident
# is the single most expensive one to answer automatically, so it is checked
# before any intent rule can claim it.
SECURITY_TERMS = (
    "security team",
    "security incident",
    "exposed",
    "leaked",
    "unauthorised access",
    "unauthorized access",
    "phishing",
    "compromised",
)

REFUND_TERMS = ("refund", "money back", "reimburse", "chargeback", "charge back", "credit us back")
CANCEL_TERMS = (
    "cancel the subscription",
    "cancel my subscription",
    "cancel the plan",
    "cancel account",
    "cancel the account",
    "cancel everything",
    "cancel the renewal",
    "terminate",
    "moving off",
)
CREDENTIAL_TERMS = (
    "locked out",
    "reset my credentials",
    "reset the credentials",
    "reset the password",
    "reset the login",
    "reset my password",
    "mfa",
    "2fa",
    "cannot log in",
    "can't log in",
    "cannot sign in",
)
ORDER_TERMS = ("order", "tracking", "shipped", "shipment", "delivery", "arrived", "courier")
INVOICE_TERMS = ("invoice", "billing", "billed", "charged", "line item")
STATUS_TERMS = ("outage", "is down", "are down", "503", "timeout", "timing out", "degraded", "stuck", "incident", "looks slow")


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(0) if match else None


def _amount_cents(text: str) -> int | None:
    match = MONEY_PATTERN.search(text)
    if not match:
        return None
    raw = match.group(1).replace(",", "")
    if "." in raw:
        whole, _, frac = raw.partition(".")
        return int(whole) * 100 + int(frac.ljust(2, "0")[:2])
    return int(raw) * 100


def _hits(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _payload(
    route: str,
    *,
    tool: str | None = None,
    args: Mapping[str, Any] | None = None,
    confidence: float,
    rationale: str,
) -> str:
    body: dict[str, Any] = {"route": route, "confidence": confidence, "rationale": rationale}
    if tool is not None:
        body["tool"] = tool
        body["args"] = dict(args or {})
    return json.dumps(body)


class OfflineSupportLLM:
    """Deterministic stand-in for the routing and drafting calls."""

    def __call__(self, request: LLMRequest) -> str:
        if request.tag == "route":
            return self._route(request.user)
        return self._compose(request.user)

    # -- routing ---------------------------------------------------------

    def _route(self, user: str) -> str:
        text = user.lower()
        order_ref = _first(ORDER_PATTERN, user)
        invoice_ref = _first(INVOICE_PATTERN, user)
        account_ref = _first(ACCOUNT_PATTERN, user)
        region = _first(REGION_PATTERN, user)
        amount = _amount_cents(user)

        if _hits(text, SECURITY_TERMS):
            return _payload(
                "escalate",
                confidence=0.9,
                rationale="the ticket reads like a security incident",
            )

        if _hits(text, ESCALATION_TERMS):
            return _payload(
                "escalate",
                confidence=0.82,
                rationale="the ticket carries legal or commercial weight",
            )

        wants_refund = _hits(text, REFUND_TERMS)
        wants_cancel = _hits(text, CANCEL_TERMS)
        wants_credentials = _hits(text, CREDENTIAL_TERMS)
        irreversible_intents = sum((wants_refund, wants_cancel, wants_credentials))

        # Two irreversible asks in one message is not a routing problem, it is
        # a judgement problem. Picking one of them is the expensive mistake.
        if irreversible_intents > 1:
            return _payload(
                "escalate",
                confidence=0.78,
                rationale="more than one irreversible action requested in a single ticket",
            )

        if wants_refund:
            if invoice_ref and amount:
                return _payload(
                    "tool",
                    tool="issue_refund",
                    args={"invoice_ref": invoice_ref, "amount_cents": amount},
                    confidence=0.88,
                    rationale="refund requested with an invoice reference and an amount",
                )
            return _payload(
                "escalate",
                confidence=0.7,
                rationale="refund requested without both an invoice reference and an amount",
            )

        if wants_cancel:
            if account_ref:
                return _payload(
                    "tool",
                    tool="cancel_subscription",
                    args={"account_ref": account_ref},
                    confidence=0.88,
                    rationale="cancellation requested with an account reference",
                )
            return _payload(
                "escalate",
                confidence=0.7,
                rationale="cancellation requested without an account reference",
            )

        if wants_credentials:
            if account_ref:
                return _payload(
                    "tool",
                    tool="reset_credentials",
                    args={"account_ref": account_ref},
                    confidence=0.86,
                    rationale="credential reset requested with an account reference",
                )
            return _payload(
                "escalate",
                confidence=0.7,
                rationale="credential reset requested without an account reference",
            )

        if _hits(text, STATUS_TERMS):
            if region:
                return _payload(
                    "tool",
                    tool="check_service_status",
                    args={"region": region},
                    confidence=0.84,
                    rationale="platform problem reported for a named region",
                )
            return _payload(
                "escalate",
                confidence=0.65,
                rationale="platform problem reported without a region",
            )

        if order_ref and _hits(text, ORDER_TERMS):
            return _payload(
                "tool",
                tool="lookup_order_status",
                args={"order_ref": order_ref},
                confidence=0.87,
                rationale="question about a specific order reference",
            )

        if invoice_ref and _hits(text, INVOICE_TERMS):
            return _payload(
                "tool",
                tool="lookup_invoice",
                args={"invoice_ref": invoice_ref},
                confidence=0.85,
                rationale="question about a specific invoice reference",
            )

        if _hits(text, ORDER_TERMS) or _hits(text, INVOICE_TERMS):
            return _payload(
                "escalate",
                confidence=0.6,
                rationale="record specific question with no usable reference",
            )

        return _payload(
            "answer",
            confidence=0.55,
            rationale="general product question answerable from documentation",
        )

    # -- drafting --------------------------------------------------------

    def _compose(self, user: str) -> str:
        try:
            context: dict[str, Any] = json.loads(user)
        except json.JSONDecodeError:
            return "Thanks for writing in. A support engineer will follow up shortly."

        outcome = context.get("outcome")
        if outcome == "acted":
            receipt = context.get("receipt") or {}
            return (
                f"Done. We ran {receipt.get('tool')} on your account and the result is "
                f"{json.dumps(receipt.get('result', {}), sort_keys=True)}. "
                f"Approved by {context.get('approver')}."
            )
        if outcome == "answered":
            receipt = context.get("receipt") or {}
            if receipt:
                return (
                    f"Here is what we found with {receipt.get('tool')}: "
                    f"{json.dumps(receipt.get('result', {}), sort_keys=True)}."
                )
            return "Thanks for writing in. That is covered in the product documentation, and here is the short version."
        if outcome == "denied":
            return (
                f"We reviewed this and are not going ahead with the requested change. "
                f"Reviewer note: {context.get('note') or 'no note supplied'}."
            )
        if outcome == "gate_rejected":
            return (
                "This request needs a human decision and the approval we received could not be verified, "
                "so nothing was executed. A support engineer will pick it up."
            )
        return "A support engineer is taking this over and will reply directly."


class ScriptedLLM:
    """Returns canned text. Use it to drive one specific branch in a test."""

    def __init__(self, responses: Mapping[str, str], default: str = "") -> None:
        self._responses = dict(responses)
        self._default = default
        self.calls: list[LLMRequest] = []

    def __call__(self, request: LLMRequest) -> str:
        self.calls.append(request)
        return self._responses.get(request.tag, self._default)


class MalformedLLM:
    """Returns something the parser cannot trust. Used to prove the fallback."""

    def __init__(self, text: str = "sure thing, refunding that now") -> None:
        self._text = text
        self.calls: list[LLMRequest] = []

    def __call__(self, request: LLMRequest) -> str:
        self.calls.append(request)
        if request.tag == "compose":
            return "A support engineer will follow up."
        return self._text
