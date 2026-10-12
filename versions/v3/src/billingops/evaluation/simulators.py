"""The simulated customer and the approval simulator."""

from __future__ import annotations

import re
from typing import Any, Mapping

DONT_KNOW = "I don't have that information."


class SimulatedUser:
    """Answers clarification questions from the disclosable fields only.

    Anything the disclosable record does not contain gets ``DONT_KNOW``. The
    simulator never sees the world or the ground truth.
    """

    def __init__(self, disclosable: Mapping[str, Any]) -> None:
        self.disclosable = dict(disclosable)
        self.asked: list[str] = []

    def answer(self, question: str) -> str:
        self.asked.append(question)
        q = question.lower()
        parts: list[str] = []
        wants_invoice = "invoice" in q or "inv-" in q
        wants_payment = "payment" in q or "pay-" in q or "transaction" in q
        wants_amount = "amount" in q or "how much" in q or "yuan" in q
        wants_date = "date" in q or "when" in q or "day" in q
        if wants_invoice and self.disclosable.get("invoice_id"):
            parts.append(f"The invoice is {self.disclosable['invoice_id']}.")
        if wants_payment and self.disclosable.get("payment_id"):
            parts.append(f"The payment id is {self.disclosable['payment_id']}.")
        if wants_amount and self.disclosable.get("amount_cents") is not None:
            parts.append(f"It was {int(self.disclosable['amount_cents']) / 100:.2f} yuan.")
        if wants_date and self.disclosable.get("date"):
            parts.append(f"It was on {self.disclosable['date']}.")
        if not parts and (wants_invoice or wants_payment) and (self.disclosable.get("invoice_id") or self.disclosable.get("payment_id")):
            # Asked for one identifier, has the other: share what is allowed.
            for key, label in (("invoice_id", "invoice"), ("payment_id", "payment id")):
                if self.disclosable.get(key):
                    parts.append(f"I only have the {label}: {self.disclosable[key]}.")
                    break
        return " ".join(parts) if parts else DONT_KNOW


class ApprovalSimulator:
    """Approves every proposal that reaches the gate. Reads nothing but the proposal."""

    def __init__(self, approver: str = "ops-sim") -> None:
        self.approver = approver
        self.approved: list[dict[str, Any]] = []

    def decide(self, proposal: Mapping[str, Any]) -> str:
        self.approved.append(dict(proposal))
        return "approve"


def mentions_forbidden(text: str, forbidden: list[str]) -> bool:
    return any(re.search(re.escape(f), text) for f in forbidden)
