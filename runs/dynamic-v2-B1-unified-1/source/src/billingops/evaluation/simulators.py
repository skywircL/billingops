"""The simulated customer and the approval simulator."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

DONT_KNOW = "I don't have that information."


class SimulatedUser:
    """Answers clarification questions from the disclosable fields only.

    Anything the disclosable record does not contain gets ``DONT_KNOW``. The
    simulator never sees the world or the ground truth. A task may instead
    supply ``scripted_answers``: the i-th question gets the i-th answer verbatim
    (a correction, an injection attempt, a "don't know"), and questions beyond
    the script fall back to the disclosable fields.
    """

    def __init__(self, disclosable: Mapping[str, Any], scripted_answers: Sequence[str] | None = None) -> None:
        self.disclosable = dict(disclosable)
        self.scripted = list(scripted_answers or [])
        self.asked: list[str] = []

    def answer(self, question: str) -> str:
        self.asked.append(question)
        if len(self.asked) <= len(self.scripted):
            return self.scripted[len(self.asked) - 1]
        q = question.lower()
        parts: list[str] = []
        wants_invoice = "invoice" in q or "inv-" in q or "账单" in q or "发票" in q
        wants_payment = "payment" in q or "pay-" in q or "transaction" in q or "付款" in q or "支付" in q or "交易" in q
        wants_amount = "amount" in q or "how much" in q or "yuan" in q or "金额" in q or "多少钱" in q
        wants_date = "date" in q or "when" in q or "day" in q or "日期" in q or "什么时候" in q or "哪天" in q
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
    """Decides every proposal that reaches the gate from a script. Reads nothing but the proposal.

    ``decision`` is ``approve`` (default) or ``reject``; ``reject_first`` lets a
    task reject the first proposal and approve a later one. The simulator never
    inspects the world or the expected outcome, so a wrong proposal is approved
    and caught by the outcome layer, exactly as a rubber-stamping operator would.
    An explicit empty or null ``note`` means no reason was supplied; it must not
    be replaced with an invented approval reason.
    """

    def __init__(self, approver: str = "ops-sim", script: Mapping[str, Any] | None = None) -> None:
        self.approver = approver
        self.script = dict(script or {"decision": "approve", "note": None})
        self.approved: list[dict[str, Any]] = []
        self.decisions: list[str] = []

    def decide(self, proposal: Mapping[str, Any]) -> str:
        n = len(self.decisions)
        decision = str(self.script.get("decision", "approve"))
        if self.script.get("reject_first") and n == 0:
            decision = "reject"
        self.decisions.append(decision)
        if decision == "approve":
            self.approved.append(dict(proposal))
        return decision

    @property
    def note(self) -> str:
        if "note" not in self.script:
            return "simulated decision"
        note = self.script["note"]
        # The human-decision API accepts strings, so null is an empty reason.
        return "" if note is None else str(note)


def mentions_forbidden(text: str, forbidden: list[str]) -> bool:
    return any(re.search(re.escape(f), text) for f in forbidden)
