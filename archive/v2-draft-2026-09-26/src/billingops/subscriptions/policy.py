"""Reference calculator for subscription_policy_v1 plus the provisional decisions of spec v2 §11.

Pure functions, no I/O. Two uses:

* an independent re-computation of the hand-written ground truth in
  ``data/tasks-v2/gold-v2.json`` (the seed literals are typed by hand from the
  spec; this module must arrive at the same numbers, otherwise one of the two
  is wrong and the seed is not admitted);
* the policy core of the v2 Billing environment once it exists.

Agreement between the two catches arithmetic slips, not a misread rule: both
derivations come from the same reading of the spec. Rule interpretation is
what the business reviewers sign off (seed ``review.status``).

Money is integer USD cents. Time is UTC. Ratios are exact fractions.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Any, Iterable, Mapping

POLICY_VERSION = "subscription_policy_v1"
PRICE_VERSION = "prices-2026-07"
DECISIONS_VERSION = "gt-decisions-v1"

REFUND_WINDOW = timedelta(days=7)   # D12: received_at - paid_at <= 168 hours, inclusive
QUOTE_TTL = timedelta(minutes=10)   # spec §4.5

# D14: every open or finished request on a payment reserves its amount.
OCCUPYING_REFUND_STATUSES = frozenset({"pending_review", "approved", "processing", "succeeded"})


@dataclass(frozen=True)
class Plan:
    name: str
    monthly_cents: int
    annual_cents: int | None
    units: int
    concurrency: int


PLANS: dict[str, Plan] = {
    "free": Plan("free", 0, None, 50, 1),
    "pro": Plan("pro", 2000, 20000, 1000, 2),
    "max_5x": Plan("max_5x", 10000, None, 5000, 5),
    "max_20x": Plan("max_20x", 20000, None, 20000, 10),
}
RANK = {"free": 0, "pro": 1, "max_5x": 2, "max_20x": 3}


# -- time --------------------------------------------------------------------------------------


def parse(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc)
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_months(ts: str | datetime, months: int) -> datetime:
    """D1: same day and time ``months`` calendar months later; a missing day clamps to the month's last day."""
    dt = parse(ts)
    year, month0 = divmod(dt.month - 1 + months, 12)
    year += dt.year
    last = calendar.monthrange(year, month0 + 1)[1]
    return dt.replace(year=year, month=month0 + 1, day=min(dt.day, last))


def cycle_months(cycle: str) -> int:
    return {"monthly": 1, "annual": 12}[cycle]


def remaining_ratio(now: str | datetime, start: str | datetime, end: str | datetime) -> Fraction:
    """D2: (end - now) / (end - start), to the second, as an exact fraction."""
    n, s, e = parse(now), parse(start), parse(end)
    if not s <= n <= e:
        raise ValueError(f"{iso(n)} is outside [{iso(s)}, {iso(e)}]")
    return Fraction(int((e - n).total_seconds()), int((e - s).total_seconds()))


# -- rounding -----------------------------------------------------------------------------------


def round_half_up(x: Fraction) -> int:
    """D3: money to the cent, halves up. Inputs are never negative here."""
    if x < 0:
        raise ValueError("negative amount")
    return int((x * 2 + 1) // 2)


def floor_units(x: Fraction) -> int:
    """D4: usage units round down, so a partial window never grants more than was paid for."""
    return x.numerator // x.denominator


# -- catalogue -----------------------------------------------------------------------------------


def price(plan: str, cycle: str) -> int:
    p = PLANS[plan]
    if cycle == "monthly":
        return p.monthly_cents
    if cycle == "annual" and p.annual_cents is not None:
        return p.annual_cents
    raise ValueError(f"{plan} is not offered {cycle}")


def available_units(granted: int, used: int, reversed_units: int = 0) -> int:
    """D4: granted minus net consumption (consumption minus reversals), never below zero."""
    return max(0, granted - (used - reversed_units))


# -- subscription changes --------------------------------------------------------------------------


def upgrade_quote(*, current_plan: str, target_plan: str, period_start: str, period_end: str,
                  window_start: str, window_end: str, granted: int, used: int, now: str,
                  reversed_units: int = 0) -> dict[str, Any]:
    """Spec §4.2: monthly -> higher monthly, effective when the proration invoice settles."""
    if current_plan == "free" or RANK[target_plan] <= RANK[current_plan]:
        raise ValueError("not an immediate monthly upgrade")
    charge = round_half_up((PLANS[target_plan].monthly_cents - PLANS[current_plan].monthly_cents)
                           * remaining_ratio(now, period_start, period_end))
    added = floor_units((PLANS[target_plan].units - PLANS[current_plan].units)
                        * remaining_ratio(now, window_start, window_end))
    return {
        "charge_cents": charge,
        "added_units": added,
        "granted_after": granted + added,
        "available_after": available_units(granted + added, used, reversed_units),
        "period_end": iso(parse(period_end)),       # unchanged
        "next_renewal_cents": PLANS[target_plan].monthly_cents,
    }


def purchase_quote(*, plan: str, cycle: str, now: str) -> dict[str, Any]:
    """Spec §4.1: Free -> paid, or a new subscription after expiry. The period starts at settlement."""
    start = parse(now)
    return {
        "cash_due_cents": price(plan, cycle),
        "period_start": iso(start),
        "period_end": iso(add_months(start, cycle_months(cycle))),
        "window_start": iso(start),
        "window_end": iso(add_months(start, 1)),
        "granted_units": PLANS[plan].units,
    }


def cross_cycle_quote(*, period_start: str, period_end: str, period_settled_cents: int, target_plan: str,
                      target_cycle: str, now: str, credit_balance_cents: int = 0) -> dict[str, Any]:
    """Spec §4.3 with D6: the unfulfilled value of the current period becomes credit, credit pays the new fee first."""
    value = round_half_up(period_settled_cents * remaining_ratio(now, period_start, period_end))
    fee = price(target_plan, target_cycle)
    credit = credit_balance_cents + value
    applied = min(credit, fee)
    start = parse(now)
    return {
        "unfulfilled_value_cents": value,
        "fee_cents": fee,
        "credit_applied_cents": applied,
        "cash_due_cents": fee - applied,
        "credit_left_cents": credit - applied,
        "period_start": iso(start),
        "period_end": iso(add_months(start, cycle_months(target_cycle))),
        "window_start": iso(start),
        "window_end": iso(add_months(start, 1)),
        "granted_units": PLANS[target_plan].units,
    }


def renewal(*, plan: str, cycle: str, at: str, credit_balance_cents: int = 0) -> dict[str, Any]:
    """What the renewal job produces at the period boundary (D25: the renewal plan is whatever the arrangement says)."""
    fee = price(plan, cycle)
    applied = min(credit_balance_cents, fee)
    start = parse(at)
    return {
        "fee_cents": fee,
        "credit_applied_cents": applied,
        "cash_due_cents": fee - applied,
        "credit_left_cents": credit_balance_cents - applied,
        "period_start": iso(start),
        "period_end": iso(add_months(start, cycle_months(cycle))),
        "window_start": iso(start),
        "window_end": iso(add_months(start, 1)),
        "granted_units": PLANS[plan].units,
    }


def expire_to_free(*, at: str) -> dict[str, Any]:
    """D24/D5: paid entitlements end at the period boundary; the account falls back to Free with a fresh window."""
    start = parse(at)
    return {"plan": "free", "window_start": iso(start), "window_end": iso(add_months(start, 1)), "granted_units": PLANS["free"].units}


# -- refunds -----------------------------------------------------------------------------------------


def within_refund_window(paid_at: str, received_at: str) -> bool:
    delta = parse(received_at) - parse(paid_at)
    return timedelta(0) <= delta <= REFUND_WINDOW


def occupied_cents(requests: Iterable[Mapping[str, Any]], payment_id: str) -> int:
    return sum(int(r["amount_cents"]) for r in requests if r["payment_id"] == payment_id and r["status"] in OCCUPYING_REFUND_STATUSES)


def unused_refund(*, payment: Mapping[str, Any], requests: Iterable[Mapping[str, Any]], net_units_since_payment: int,
                  received_at: str) -> dict[str, Any]:
    """Spec §5.1 rows 1-2 with D12-D16: a settled charge, inside the window, unused, with something left to refund."""
    if payment.get("status") != "succeeded" or payment.get("kind", "charge") != "charge":
        return {"eligible": False, "reason": "not_a_settled_charge"}
    if not within_refund_window(payment["paid_at"], received_at):
        return {"eligible": False, "reason": "outside_window"}
    if net_units_since_payment > 0:
        return {"eligible": False, "reason": "used"}
    amount = int(payment["amount_cents"]) - occupied_cents(requests, payment["id"])
    if amount <= 0:
        return {"eligible": False, "reason": "nothing_left_to_refund"}
    return {
        "eligible": True,
        "amount_cents": amount,
        "plan": {"entitlement_action": "terminate_covered", "effective": "on_execution", "stop_renewal": True},
    }


def duplicate_payments(payments: Iterable[Mapping[str, Any]], invoice_id: str) -> list[dict[str, Any]]:
    """Spec §5.1 duplicate row: succeeded charges on one invoice, oldest first; every one after the first is extra.

    Authorizations, voided holds, failed and pending attempts are not charges (F-06).
    """
    charges = sorted(
        (dict(p) for p in payments if p["invoice_id"] == invoice_id and p.get("kind", "charge") == "charge" and p["status"] == "succeeded"),
        key=lambda p: (p["paid_at"], p["id"]),
    )
    return charges[1:]


def overcharge_cents(*, invoice_amount_cents: int, plan: str, cycle: str) -> int:
    """Spec §5.1 overcharge row: what was invoiced above the catalogue price of that price version."""
    return max(0, int(invoice_amount_cents) - price(plan, cycle))
