"""Establish which charge the customer means before money eligibility is checked.

A model's proposed id is never itself evidence of customer intent. With no
user identifier, every listed invoice must have been inspected and exactly
one extra payment must exist. Refundability is a separate decision.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .evidence import EvidenceIndex
from .authorization import resolve_customer_authorization

# Boundaries are ASCII alphanumerics, not ``\b``: Chinese customers often write an id right next to a CJK character
# ("账单INV-2026-0142扣了两次"), which ``\b`` does not treat as a boundary (coverage-v1 acceptance, 2026-09-27).
INVOICE_RE = re.compile(r"(?<![A-Za-z0-9])INV-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])")
PAYMENT_RE = re.compile(r"(?<![A-Za-z0-9])PAY-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])")


def user_ids(text: str, answers: list[str]) -> tuple[list[str], list[str]]:
    # A later answer containing ids is a correction, not an additional target.
    for utterance in [*reversed(answers), text]:
        invoices = sorted(set(INVOICE_RE.findall(utterance)))
        payments = sorted(set(PAYMENT_RE.findall(utterance)))
        if invoices or payments:
            return invoices, payments
    return [], []


def extra_payments(payments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((p for p in payments if p["status"] == "succeeded"), key=lambda p: (p["paid_at"], p["id"]))[1:]


def resolve_target(state: dict[str, Any], idx: EvidenceIndex, invoice_id: str | None,
                   payment_id: str | None) -> dict[str, Any]:
    answers = [c["answer"] for c in state.get("clarifications", [])] + list(state.get("extra_info", []))
    invoices, payments = user_ids(state["text"], answers)
    def result(ok: bool, reason: str, scope: str = "explicit", ids: list[str] | None = None) -> dict[str, Any]:
        return {"ok": ok, "reason": reason, "scope": scope, "invoice_ids": ids or [], "payment_id": payment_id}

    if len(payments) > 1 or len(invoices) > 1:
        return result(False, "ambiguous_customer_target")
    if payments:
        if payment_id != payments[0] or payment_id not in idx.payments:
            return result(False, "customer_payment_mismatch")
        if invoices and invoices[0] != invoice_id:
            # A typo in an invoice can be corrected by a valid named payment,
            # but only after the typo has actually been checked and not found.
            not_found = any(e.get("tool") == "get_invoice" and e.get("args", {}).get("invoice_id") == invoices[0]
                            and not e.get("ok") and str(e.get("error", "")).startswith("not_found:")
                            for e in state.get("evidence", []))
            if not not_found:
                return result(False, "conflicting_customer_identifiers")
        return result(True, "customer_named_payment", ids=[str(invoice_id)])
    if invoices:
        if invoice_id != invoices[0]:
            return result(False, "customer_invoice_mismatch")
        candidate_ids = invoices
        scope = "invoice"
    else:
        if idx.invoice_list is None:
            return result(False, "target_scope_not_read", "account")
        candidate_ids = sorted(i["id"] for i in idx.invoice_list)
        scope = "account"
    if any(i not in idx.payments_by_invoice for i in candidate_ids):
        return result(False, "target_scope_incomplete", scope, candidate_ids)
    extras = [p for i in candidate_ids for p in extra_payments(idx.payments_by_invoice[i])]
    if len(extras) != 1:
        return result(False, "ambiguous_customer_target" if extras else "no_unique_duplicate_target", scope, candidate_ids)
    if extras[0]["id"] != payment_id or extras[0]["invoice_id"] != invoice_id:
        return result(False, "customer_target_mismatch", scope, candidate_ids)
    return result(True, "unique_duplicate_target", scope, candidate_ids)


_INQUIRY_HOLD = re.compile(
    r"退款.{0,8}(?:别|不要)(?:办|处理|弄|动)|"
    r"(?:先|暂时|暂)(?:别|不要|不)(?:办|处理|发起)?退款|"
    r"(?:不要|别)退款(?:[，。！!,. ]|$)|只(?:要|需)?(?:查|核查|核对|调查|解释)|"
    r"(?:hold off|defer|postpone).{0,25}refund|"
    r"(?:do not|don't)\s+(?:issue|start|process|send|make|initiate)\s+(?:a\s+|any\s+|the\s+)?refund|"
    r"(?:no|without)\s+refund\s+(?:yet|for now)", re.I)
def _customer_utterances(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [{"text": str(state.get("text", "")), "source": "opening", "command_id": None}]
    if 'authorization_utterances' in state:
        return rows + list(state['authorization_utterances'])
    for row in state.get("clarifications", []):
        rows.append({"text": str(row.get("answer", "")), "source": "clarification",
                     "command_id": row.get("command_id")})
    for row in state.get("extra_info", []):
        rows.append({"text": str(row), "source": "customer_followup", "command_id": None})
    return rows


def _month_supported(month: str, text: str, as_of: str | None) -> bool:
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", month):
        return False
    year, number = month.split("-")
    if month in text or re.search(rf"{year}\s*年\s*0?{int(number)}\s*月", text):
        return True
    names = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
    if re.search(rf"\b{names[int(number)-1]}\b", text, re.I) and year in text:
        return True
    if as_of and re.search(r"本月|这个月|this\s+month|current\s+month", text, re.I):
        try:
            return datetime.fromisoformat(as_of).strftime("%Y-%m") == month
        except ValueError:
            return False
    return False


def resolve_invoice_target(
    state: dict[str, Any], idx: EvidenceIndex, invoice_id: str | None,
    payment_ids: list[str] | None = None, *, as_of: str | None = None,
) -> dict[str, Any]:
    """V2 customer scope, independent of which payments can be refunded.

    The model's invoice/payment candidates cannot create authorization. IDs are
    read from actual customer utterances; a reference must resolve through
    customer-bound Billing evidence. A later customer ID correction supersedes
    earlier IDs, and its durable clarification command is retained as evidence.
    Invoice scope permits all verified excess payments on that ONE invoice.
    The fixed/legacy ``resolve_target`` API above remains unchanged.
    """
    utterances = _customer_utterances(state)
    extracted = state.get("extracted") or {}
    action = str(extracted.get("request_action", "unknown"))
    role = str(extracted.get("payment_reference_role", "unspecified"))
    if action not in {"refund", "inquire", "unknown"}:
        action = "unknown"
    if role not in {"locator", "exclusive", "unspecified"}:
        role = "unspecified"
    all_text = "\n".join(row["text"] for row in utterances)
    customer_scope = resolve_customer_authorization(utterances, role)
    # An explicit hold is a negative authorization boundary, even if an LLM
    # mislabeled request_action. A later affirmative scope confirmation is
    # handled as its own persisted graph action, not an LLM 'confirmed' flag.
    excluded = set(customer_scope['explicitly_excluded_payment_ids'])
    hold_text = '\n'.join(part for part in re.split(r'[,，。;；]', all_text)
                          if not (set(PAYMENT_RE.findall(part)) and set(PAYMENT_RE.findall(part)) <= excluded))
    if _INQUIRY_HOLD.search(hold_text):
        action = "inquire"
    source = utterances[0]
    invoices: list[str] = []
    references: list[str] = []
    for row in reversed(utterances):
        inv = sorted(set(INVOICE_RE.findall(row["text"])))
        pay = sorted(set(PAYMENT_RE.findall(row["text"])))
        if inv or pay:
            source, invoices, references = row, inv, pay
            break
    if customer_scope['scope_mode'] != 'invoice_all':
        role = "exclusive"
    commands = [str(row["command_id"]) for row in utterances if row.get("command_id")]
    authorization = {"refund": "refund_requested", "inquire": "refund_not_requested", "unknown": "unresolved"}[action]
    requested: list[str] = customer_scope['requested_payment_ids']
    base: dict[str, Any] = {
        "scope": "invoice", "invoice_ids": [], "payment_id": None,
        "request_action": action, "authorization": authorization,
        "payment_reference_role": role, "requested_payment_ids": requested,
        "source_utterance": dict(source), "source_command_ids": commands,
        "scope_month": extracted.get("scope_month"), "scope_plan": extracted.get("scope_plan"),
        "authorization_scope": customer_scope,
    }

    def result(ok: bool, reason: str, **extra: Any) -> dict[str, Any]:
        return {**base, "ok": ok, "reason": reason, **extra}

    if action == "unknown":
        return result(False, "customer_action_unresolved")
    if len(invoices) > 1:
        return result(False, "ambiguous_customer_invoice_scope")
    if action == 'refund' and customer_scope['resolution_status'] != 'resolved':
        return result(False, customer_scope['reason'])
    if references:
        if any(pid not in idx.payments for pid in references):
            return result(False, "payment_reference_not_read")
        if any(idx.payments[pid].get("customer_id") != state["customer_id"] for pid in references):
            return result(False, "payment_not_owned_by_customer")
        located = {str(idx.payments[pid].get("invoice_id")) for pid in references}
        if len(located) != 1:
            return result(False, "ambiguous_customer_invoice_scope")
        located_invoice = next(iter(located))
        if invoices and invoices[0] != located_invoice:
            invalid_named_invoice = any(e.get("tool") == "get_invoice" and e.get("args", {}).get("invoice_id") == invoices[0]
                                        and e.get("ok") is False and str(e.get("error", "")).startswith("not_found:")
                                        for e in state.get("evidence", []))
            if not invalid_named_invoice:
                return result(False, "conflicting_customer_identifiers")
        if invoice_id is not None and invoice_id != located_invoice:
            return result(False, "customer_invoice_mismatch")
        # Unspecified references do not grant a wider refund scope. The model
        # must classify them from public text; explicit restrictions override.
        if role == "unspecified" and action == "refund":
            return result(False, "payment_reference_role_unresolved")
        if role == "exclusive" and payment_ids and set(payment_ids) != set(requested):
            return result(False, "customer_payment_mismatch")
        base.update(scope="explicit_payment" if role == "exclusive" else "invoice",
                    invoice_ids=[located_invoice], payment_id=requested[0] if role == "exclusive" and requested else None)
        chosen = located_invoice
    elif invoices:
        if invoice_id is not None and invoice_id != invoices[0]:
            return result(False, "customer_invoice_mismatch")
        chosen = invoices[0]
        base["invoice_ids"] = [chosen]
    else:
        if idx.invoice_list is None:
            return result(False, "target_scope_not_read", scope="account")
        candidates = [row for row in idx.invoice_list if row.get("customer_id") == state["customer_id"]]
        month, plan = extracted.get("scope_month"), extracted.get("scope_plan")
        base["scope"] = "month_plan" if month or plan else "account"
        if month:
            if not isinstance(month, str) or not _month_supported(month, all_text, as_of or state.get("as_of")):
                return result(False, "scope_month_not_supported_by_customer")
            candidates = [row for row in candidates if str(row.get("period_start", "")).startswith(month)]
        if plan:
            if not isinstance(plan, str) or plan.casefold() not in all_text.casefold():
                return result(False, "scope_plan_not_supported_by_customer")
            if idx.subscription_history is None:
                return result(False, "subscription_scope_not_read")
            normalized = plan.casefold().strip()
            subs = {row["id"] for row in idx.subscription_history.get("subscriptions", [])
                    if row.get("customer_id") == state["customer_id"] and str(row.get("plan", "")).casefold() == normalized}
            candidates = [row for row in candidates if row.get("subscription_id") in subs]
        candidate_ids = sorted(str(row["id"]) for row in candidates)
        base["candidate_invoice_ids"] = candidate_ids
        if any(iid not in idx.payments_by_invoice for iid in candidate_ids):
            return result(False, "target_scope_incomplete", invoice_ids=candidate_ids)
        duplicated = [iid for iid in candidate_ids if len(extra_payments(idx.payments_by_invoice[iid])) > 0]
        if len(candidates) == 1:
            chosen = candidate_ids[0]
        elif len(duplicated) == 1:
            chosen = duplicated[0]
        else:
            return result(False, "ambiguous_customer_invoice_scope" if duplicated else "no_unique_duplicate_target", invoice_ids=candidate_ids)
        if invoice_id is not None and invoice_id != chosen:
            return result(False, "customer_invoice_mismatch", invoice_ids=candidate_ids)
        base["invoice_ids"] = [chosen]
    invoice = idx.invoices.get(chosen)
    if invoice is None:
        return result(False, "invoice_not_read")
    if invoice.get("customer_id") != state["customer_id"]:
        return result(False, "invoice_not_owned_by_customer")
    if chosen not in idx.payments_by_invoice:
        return result(False, "target_scope_incomplete")
    return result(True, "customer_scope_resolved", invoice_id=chosen)
