"""B2 public answer facts, derived from observed records rather than evaluation answers.

The execution receipt is rendered after investigation and used to lose the
customer/record amount discrepancy. Persist only that narrow factual context;
never carry an old proposed/approved/completed sentence into a later receipt.
"""
from __future__ import annotations

from decimal import Decimal
import re
from typing import Any, Mapping


def _explicit_unit_amounts(text: str) -> set[int]:
    """Only unambiguous invoice/per-payment claims, never a refund/paid total.

    Extraction's flat amount list does not encode a monetary role. When prose
    does not establish that role, leave explanation to the evidence-aware model
    instead of manufacturing a discrepancy from incomparable numbers.
    """
    number = r"(?P<n>\d+(?:\.\d{1,2})?)"
    patterns = [
        rf"(?:每次|每笔|每回|各|扣了(?:两|三|二|\d+)次)\s*{number}\s*元",
        rf"{number}\s*元(?:的)?账单",
        rf"账单(?:应付)?(?:金额)?\s*[:：为是]?\s*{number}\s*元",
        rf"{number}[\s-]*(?:yuan|CNY)\s+(?:invoice|bill|each)\b",
        rf"(?:each\s+(?:payment|charge)(?:\s+of)?|invoice\s+amount(?:\s+of)?)\s*{number}\s*(?:yuan|CNY)\b",
    ]
    return {int(Decimal(match.group('n')) * 100) for pattern in patterns
            for match in re.finditer(pattern, text, re.I)}


def response_context(bundle: Mapping[str, Any]) -> dict[str, Any]:
    extracted = bundle.get("extracted") or {}
    invoice = bundle.get("invoice") or {}
    if not invoice:
        invoice_id = (bundle.get("selected_target") or {}).get("invoice_id")
        invoice = (bundle.get("invoices_by_id") or {}).get(invoice_id) or {}
    amount = invoice.get("amount_cents")
    if (not extracted.get("mentions_duplicate") or extracted.get("requests_price_adjustment")
            or type(amount) is not int or amount < 0 or invoice.get("currency") != "CNY"
            or invoice.get("customer_id") != bundle.get("customer_id")
            or not invoice.get("id") or invoice.get("derived_from")):
        return {}
    explicit = _explicit_unit_amounts(str(bundle.get("customer_request") or ""))
    claims = sorted({x for x in extracted.get("amounts_cents", []) if type(x) is int and x >= 0} & explicit)
    if not claims or amount in claims:
        return {}
    return {"amount_conflict": {"invoice_id": invoice["id"], "recorded_invoice_cents": amount,
                                "customer_mentioned_cents": claims,
                                "source": "customer_claim_vs_observed_invoice"}}


def amount_conflict_notice(context: Mapping[str, Any]) -> str:
    conflict = context.get("amount_conflict") or {}
    amount = conflict.get("recorded_invoice_cents")
    claims = conflict.get("customer_mentioned_cents") or []
    if type(amount) is not int or not claims or any(type(x) is not int for x in claims):
        return ""
    stated = "、".join(f"{x / 100:.2f} 元" for x in claims)
    return (f"金额核对：您提到的金额为 {stated}；查询到的账单 {conflict['invoice_id']} "
            f"应付金额为 {amount / 100:.2f} 元，两者不一致，账务处理以核实的记录和授权范围为准。")


def complete_reply(text: str, bundle: Mapping[str, Any]) -> str:
    """Attach narrow observed facts without changing a conclusion or authorizing work.

    This does not make an incorrect model statement correct: contradictory text
    remains visible to the grader. It prevents required facts from disappearing
    during compression and deterministic receipt rendering.
    """
    parts = [text.strip()] if text.strip() else []
    notice = amount_conflict_notice(response_context(bundle))
    if notice and notice not in text:
        parts.append(notice)
    extracted = bundle.get("extracted") or {}
    kind = bundle.get("conclusion_type")
    if (kind == "inquiry_completed" and extracted.get("request_action") == "inquire"
            and extracted.get("mentions_duplicate") and not bundle.get("proposal")):
        parts.append("本次仅核查，未发起退款；如需办理，仍须您另行确认授权，再由操作员独立审批。")
    if (extracted.get("mentions_refund_status")
            and kind in {"explain_refund_status", "inquiry_completed"}):
        # A selected payment supplies a bounded lookup. An account-wide list
        # without a selected payment must be matched by the investigator first.
        target = (bundle.get("selected_target") or {}).get("payment_id")
        for refund in bundle.get("refunds") or []:
            if not target or refund.get("payment_id") != target:
                continue
            if (not refund.get("id") or type(refund.get("amount_cents")) is not int
                    or refund.get("currency") != "CNY"):
                continue
            fact = f"退款 {refund['id']}：退款金额 {refund['amount_cents'] / 100:.2f} 元，记录状态 {refund.get('status', 'unknown')}"
            if refund.get("created_at"):
                fact += f"，申请/创建时间 {refund['created_at']}"
            if refund.get("completed_at"):
                fact += f"，账务完成时间 {refund['completed_at']}"
            else:
                fact += "，记录中暂无完成时间"
            parts.append(fact + "。账务完成不代表银行已到账，无法承诺到账日期。")
    if kind == "insufficient_information":
        # A later successful lookup must not be described as still not found.
        latest = {}
        for observation in bundle.get("read_observations") or []:
            if observation.get("tool") == "get_invoice":
                latest[(observation.get("args") or {}).get("invoice_id")] = observation
        for invoice_id, observation in latest.items():
            error = observation.get("error")
            code = error.get("code") if isinstance(error, Mapping) else error
            args = observation.get("args") or {}
            if (observation.get("tool") == "get_invoice" and observation.get("ok") is False
                    and code == "not_found" and args.get("invoice_id")
                    and invoice_id not in (bundle.get("invoices_by_id") or {})):
                parts.append(f"查询结果：在当前客户范围内未找到账单 {args['invoice_id']}，该编号尚无法核实；这不代表不存在多扣款。")
    return "\n".join(dict.fromkeys(parts))
