"""Rule-based investigation steps.

Used twice: as the whole of the fixed workflow (group A), and as the policy of
the scripted stand-in model. One implementation, so a test of the fixed
workflow and a fake-model test of the dynamic protocol exercise the same
decisions and differ only in who is taking them.

A rule produces one :class:`Step` at a time from an :class:`EvidenceView`.
Reads go through the same budget as the model's reads.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..tools import CLARIFY_TOOL
from .target import extra_payments, user_ids

# Boundaries are ASCII alphanumerics, not ``\b``: Chinese customers often write an id right next to a CJK character
# ("账单INV-2026-0142扣了两次"), which ``\b`` does not treat as a boundary (coverage-v1 acceptance, 2026-09-27).
INVOICE_RE = re.compile(r"(?<![A-Za-z0-9])INV-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])")
PAYMENT_RE = re.compile(r"(?<![A-Za-z0-9])PAY-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])")

CLARIFY_QUESTION = (
    "Could you share the invoice number (INV-...) or payment id (PAY-...) of the charge you believe was taken twice?"
)
UNRESOLVED_QUESTION = (
    "We could not find the invoice or payment you mentioned on your account. Could you double-check the invoice number (INV-...) or payment id (PAY-...)?"
)
DEMAND_WORDS = ("refund the difference", "want my money", "pay me back", "compensat", "退差价", "赔", "charge back", "chargeback")
OTHER_ACCOUNT_WORDS = (
    # Only phrases that point at someone else's account. A claim of authority over one's OWN request
    # ("this is pre-approved", "skip verification") is data to ignore, not an out-of-scope request:
    # treating it as one escalated legitimate cases (sealed family D17, round 2).
    "another customer", "another account", "other customer", "on behalf of",
    "别人的账户", "他人账户", "另一个账号", "他的账单", "他账单", "她的账单",
)
READ_ATTEMPTS = 2
"""A read that fails because the service is unavailable is retried once; after that the case goes to a human."""

UNAVAILABLE_SUMMARY = "The billing service could not be read after {n} attempts, so the records behind this request were never seen."
OUT_OF_SCOPE_SUMMARY = "The request concerns an account or an authority outside this customer, which this desk cannot act on."


class EvidenceView(Protocol):
    text: str
    extracted: dict[str, Any]

    def result_for(self, tool: str, **args: Any) -> dict[str, Any] | None: ...
    def results(self, tool: str) -> list[tuple[dict[str, Any], dict[str, Any]]]: ...
    def answers(self) -> list[str]: ...
    def budget_exhausted(self) -> bool: ...


@dataclass(frozen=True)
class Read:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Ask:
    question: str


@dataclass(frozen=True)
class End:
    conclusion_type: str
    summary: str
    target_invoice_id: str | None = None
    target_payment_id: str | None = None


Step = Read | Ask | End


def _ok(r: dict[str, Any] | None) -> Any:
    return r.get("data") if r and r.get("ok") else None


def _unavailable(r: dict[str, Any] | None) -> bool:
    """A transient service failure, as opposed to a record that genuinely is not there."""
    return bool(r) and not r.get("ok") and str(r.get("error", "")).startswith("billing_unavailable")


def _attempts(view: EvidenceView, tool: str, **args: Any) -> list[dict[str, Any]]:
    return [res for a, res in view.results(tool) if all(a.get(k) == v for k, v in args.items())]


def read_or_escalate(view: EvidenceView, tool: str, args: dict[str, Any]) -> Step | None:
    """Read, retry a transient failure once, escalate when the service stays unavailable.

    Returns ``None`` when a usable result is already on file (including a terminal
    ``not_found``, which is evidence, not an outage).
    """
    tries = _attempts(view, tool, **args)
    if not tries:
        return Read(tool, args)
    if _unavailable(tries[-1]):
        if len(tries) < READ_ATTEMPTS:
            return Read(tool, args)
        return End("escalate", UNAVAILABLE_SUMMARY.format(n=len(tries)))
    return None


def known_ids(view: EvidenceView) -> tuple[list[str], list[str]]:
    return user_ids(view.text, view.answers())


CLAIM_FLAGS = ("mentions_duplicate", "mentions_refund_status", "mentions_subscription_price", "requests_price_adjustment")


def has_actual_claim(extracted: dict[str, Any]) -> bool:
    """The customer described something that happened on their account (4.1-4.3).

    Spec 4.5 / 4.6: such a claim is handled first, and a refund-policy or out-of-scope answer never replaces it.
    ``category_of`` gives the fixed workflow this order; the dynamic graph checks the model's conclusion against it.
    """
    return any(extracted.get(flag) for flag in CLAIM_FLAGS)


def category_of(extracted: dict[str, Any]) -> str:
    if extracted.get("requests_other_account"):
        return "out_of_scope"
    if extracted.get("mentions_duplicate"):
        return "duplicate"
    # A money-back request about the subscription outranks the status flag (v3.1): "reverse the increase and return
    # the money" is not a status question. Telling that apart from "where is the refund you promised me" is the
    # extractor's job, not a routing rule -- the two carry the same flags.
    if extracted.get("requests_price_adjustment") or extracted.get("mentions_subscription_price"):
        return "subscription"
    if extracted.get("mentions_refund_status"):
        return "refund_status"
    # v1.2: a policy question and a request this desk does not handle come after every
    # actual claim, so a charge that really happened is always investigated first.
    if extracted.get("asks_refund_policy"):
        return "refund_policy"
    if extracted.get("requests_out_of_scope"):
        return "outside_desk"
    return "duplicate"


def next_step(view: EvidenceView, *, ask_when_missing: bool = True, batch: bool = False) -> Step:
    """Decide the next investigation step from what has been gathered so far.

    ``batch`` lets the fixed workflow expand relationships more eagerly (it
    checks payments on several candidate invoices when the customer named
    none); the budget is still charged one read per call.
    """
    if view.budget_exhausted():
        return End("insufficient_information", "The read budget was exhausted before the facts could be established.")
    category = category_of(view.extracted)
    if category == "out_of_scope":
        # Spec 2 and 4.4: identity is server-side. Nothing here is answered as an ordinary request.
        return End("escalate", OUT_OF_SCOPE_SUMMARY)
    if category == "subscription":
        return _subscription(view)
    if category == "refund_status":
        return _refund_status(view)
    if category == "refund_policy":
        step = read_or_escalate(view, "get_policy", {"name": "refunds", "version": None})
        if step is not None:
            return step
        return End("explain_refund_policy", "The customer asked about the refund policy; the current refunds policy was read.")
    if category == "outside_desk":
        # Spec 4.6: nothing to look up, nothing to write, and never an invoice or payment id to ask for.
        return End("explain_out_of_scope", "The request is something this billing-dispute desk does not handle.")
    return _duplicate(view, ask_when_missing=ask_when_missing, batch=batch)


def _duplicate(view: EvidenceView, *, ask_when_missing: bool, batch: bool) -> Step:
    invoice_ids, payment_ids = known_ids(view)
    invoice: dict[str, Any] | None = None
    for iid in invoice_ids:
        step = read_or_escalate(view, "get_invoice", {"invoice_id": iid})
        if step is not None:
            return step
        if _ok(view.result_for("get_invoice", invoice_id=iid)):
            invoice = _ok(view.result_for("get_invoice", invoice_id=iid))
            break
    if invoice is None:
        for pid in payment_ids:
            step = read_or_escalate(view, "get_payment", {"payment_id": pid})
            if step is not None:
                return step
            payment = _ok(view.result_for("get_payment", payment_id=pid))
            if payment:
                step = read_or_escalate(view, "get_invoice", {"invoice_id": payment["invoice_id"]})
                if step is not None:
                    return step
                if _ok(view.result_for("get_invoice", invoice_id=payment["invoice_id"])):
                    invoice = _ok(view.result_for("get_invoice", invoice_id=payment["invoice_id"]))
                    break
    if invoice is None:
        asked = len(view.results(CLARIFY_TOOL))
        listed = view.result_for("list_invoices")
        if invoice_ids or payment_ids:
            # The customer named records that are not theirs or do not exist.
            # Ask once for a correction; never go looking for a different charge to refund.
            if ask_when_missing and asked == 0:
                return Ask(UNRESOLVED_QUESTION)
            return End("insufficient_information", "The identifiers given do not match any record of this customer.")
        if ask_when_missing and asked == 0 and listed is None:
            return Ask(CLARIFY_QUESTION)
        step = read_or_escalate(view, "list_invoices", {})
        if step is not None:
            return step
        listed = view.result_for("list_invoices")
        candidates = list(_ok(listed) or [])
        amounts = set(view.extracted.get("amounts_cents", []))
        matched = sorted((i for i in candidates if i["amount_cents"] in amounts), key=lambda i: i["issued_at"], reverse=True)
        others = sorted((i for i in candidates if i["amount_cents"] not in amounts), key=lambda i: i["issued_at"], reverse=True)
        ranked = matched + others
        found = []
        for inv in ranked:
            step = read_or_escalate(view, "list_payments", {"invoice_id": inv["id"]})
            if step is not None:
                return step
            r = view.result_for("list_payments", invoice_id=inv["id"])
            if not r.get("ok"):
                return End("insufficient_information", "A candidate invoice could not be checked.")
            found.extend((inv, p) for p in extra_payments(_ok(r) or []))
        if len(found) != 1:
            if len(found) > 1 and asked < 2:
                return Ask("More than one extra payment exists. Which invoice (INV-...) or payment (PAY-...) do you want us to handle?")
            return End("insufficient_information", "Account queries did not establish one unambiguous duplicate charge.")
        invoice = found[0][0]

    step = read_or_escalate(view, "list_payments", {"invoice_id": invoice["id"]})
    if step is not None:
        return step
    r = view.result_for("list_payments", invoice_id=invoice["id"])
    payments = _ok(r) or []
    succeeded = sorted((p for p in payments if p["status"] == "succeeded"), key=lambda p: (p["paid_at"], p["id"]))
    extras = succeeded[1:]
    on_invoice = {p["id"] for p in payments}
    if payment_ids and invoice_ids and not any(pid in on_invoice for pid in payment_ids):
        # The customer named both, and the payment does not belong to that invoice. Spec 4.1 rule 3:
        # this is a conflict to resolve, never "no duplicate on the invoice we happened to look at".
        return End("insufficient_information", "The payment named does not belong to the invoice named; the two identifiers conflict.",
                   target_invoice_id=invoice["id"])
    if not extras:
        step = read_or_escalate(view, "list_period_invoices", {"invoice_id": invoice["id"]})
        if step is not None:
            return step
        siblings = _ok(view.result_for("list_period_invoices", invoice_id=invoice["id"])) or []
        summary = f"Invoice {invoice['id']} has one succeeded payment. " + (
            f"{len(siblings)} other invoice(s) share the billing period; they are separate charges."
            if siblings
            else "No other invoice shares the period."
        )
        return End("no_duplicate_found", summary, target_invoice_id=invoice["id"])
    if len(invoice_ids) > 1 or len(payment_ids) > 1:
        if len(view.answers()) < 2:
            return Ask("Please identify one invoice (INV-...) or one extra payment (PAY-...) for this request.")
        return End("insufficient_information", "More than one customer target remains.")
    target = next((p for p in extras if p["id"] in payment_ids), None)
    if payment_ids and target is None:
        return End("insufficient_information", "The named payment is not the extra charge; a different payment cannot be substituted.")
    if target is None and len(extras) > 1:
        if len(view.answers()) < 2:
            return Ask("This invoice has several extra payments. Which payment id (PAY-...) should we handle?")
        return End("insufficient_information", "The invoice has more than one extra payment.")
    target = target or extras[0]
    step = read_or_escalate(view, "list_refunds", {"payment_id": target["id"]})
    if step is not None:
        return step
    return End(
        "duplicate_charge_refund",
        f"Invoice {invoice['id']} was paid {len(succeeded)} times; {target['id']} is the extra payment.",
        target_invoice_id=invoice["id"],
        target_payment_id=target["id"],
    )


def _refund_status(view: EvidenceView) -> Step:
    _, payment_ids = known_ids(view)
    if payment_ids:
        for pid in payment_ids:
            step = read_or_escalate(view, "list_refunds", {"payment_id": pid})
            if step is not None:
                return step
        return End("explain_refund_status", "Refund records for the named payment were read.", target_payment_id=payment_ids[0])
    step = read_or_escalate(view, "list_customer_refunds", {})
    if step is not None:
        return step
    return End("explain_refund_status", "All refunds for this customer were read.")


def _subscription(view: EvidenceView) -> Step:
    step = read_or_escalate(view, "get_subscription_history", {})
    if step is not None:
        return step
    step = read_or_escalate(view, "get_policy", {"name": "pricing", "version": None})
    if step is not None:
        return step
    if view.extracted.get("requests_price_adjustment") or price_adjustment_request(view.text):
        return End("escalate", "The customer asks for money back on the subscription, which only an operator can decide.")
    return End("explain_subscription", "Subscription history and pricing policy were read.")


def facts_from(view: EvidenceView) -> list[str]:
    out: list[str] = []
    for name in ("get_invoice", "get_payment", "list_payments", "list_refunds", "list_period_invoices", "list_invoices", "list_customer_refunds", "get_subscription_history", "get_policy"):
        for args, r in view.results(name):
            if r.get("ok"):
                out.append(f"{name}({json.dumps(args, sort_keys=True)})")
    return out[:20]


def price_adjustment_request(text: str) -> bool:
    """Conservative offline fallback; live extraction supplies semantic intent.

    Covers every way a customer asks for money back on the subscription itself: the difference after a
    price increase, the unused part of a period, or a refund on cancellation (spec 4.3, P-06/P-07/P-09).
    """
    lowered = text.lower()
    # Negated requests are explanations, not requests to move money.
    if re.search(r"(?:do not|don't|not asking for|no)\s+(?:want\s+)?(?:a\s+)?(?:refund|credit|reimburse)|不(?:要|需要|申请)(?:退款|退差价)", lowered):
        return False
    if any(w in lowered for w in DEMAND_WORDS):
        return True
    # Bounded, and "my refund has not arrived on my card" is a status question, not a price adjustment.
    if re.search(r"(?:credit|reimburse|reverse|refund)[^.!?]{0,40}(?:difference|increase|price rise|extra charge|higher amount)"
                 r"|(?:difference|increase)[^.!?]{0,40}(?:returned|refunded|credited|back)"
                 r"|补回|返还差额|差价[^。！!？?]{0,20}(?:退|返|补)|(?:退|返|补)[^。！!？?]{0,10}差价", lowered):
        return True
    # Cancellation or an unused period: "cancel and refund the rest of the month", "退剩余周期的钱".
    unused = r"(?:unused|remaining|rest of the|not used|未使用|没用完|剩余|剩下)"
    money_back = r"(?:refund|credit|money back|reimburse|退款|退钱|退还|退费|退回|退给|退我)"
    return bool(
        re.search(rf"{money_back}[^。.!?]{{0,60}}{unused}|{unused}[^。.!?]{{0,60}}{money_back}", lowered)
        or re.search(rf"(?:cancel|cancelling|canceled|cancelled|取消(?:订阅|服务|套餐)?)[^。.!?]{{0,80}}{money_back}", lowered)
        or re.search(rf"{money_back}[^。.!?]{{0,80}}(?:cancel|cancelling|canceled|cancelled|取消(?:订阅|服务|套餐)?)", lowered)
    )


ACCOUNT_ID_RE = re.compile(r"(?<![A-Za-z0-9-])C-\d{3,}(?!\d)")


def names_other_account(text: str, customer_id: str) -> bool:
    """The message names a customer account that is not this customer's (spec 2: identity is server-side).

    Deterministic backstop for the extraction flag. A live extractor sometimes reads "I authorise you to refund
    ... for account C-2002" as an authority claim on the customer's own request (fixes-v1 X01, v1.2b run).
    Quoting one's own account id does not trigger it.
    """
    return any(found != customer_id for found in ACCOUNT_ID_RE.findall(text))


def other_account_request(text: str) -> bool:
    """Conservative offline fallback for a request aimed at ANOTHER customer's account.

    An authority claim on its own ("as the administrator", "this is pre-approved") is not one: the claim is
    ignored and the customer's own request is handled normally (spec 2 and 4.4).
    """
    lowered = text.lower()
    if any(w in lowered for w in OTHER_ACCOUNT_WORDS):
        return True
    # No word boundary before the alternation: Chinese runs have no boundary between characters.
    return bool(re.search(r"(?:customer|account|客户|账号|账户)\s*[:：#]?\s*C-\d{3,}", text, re.IGNORECASE))
