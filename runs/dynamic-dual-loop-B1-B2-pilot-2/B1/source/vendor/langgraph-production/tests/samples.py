"""Sample tickets shared by several test modules."""

from __future__ import annotations

from datetime import datetime, timezone

from langgraph_production.models import Ticket

FIXED_TIME = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

REFUND_TICKET = Ticket(
    id="T-REFUND",
    text="Please refund $45.00 from invoice INV-10032, we were double charged.",
    account_tier="pro",
)
LOOKUP_TICKET = Ticket(
    id="T-LOOKUP",
    text="Order ORD-40021 has not arrived yet, can you check where it is?",
    account_tier="pro",
)
ANSWER_TICKET = Ticket(
    id="T-ANSWER",
    text="How do I export a dashboard to CSV?",
    account_tier="free",
)
ESCALATE_TICKET = Ticket(
    id="T-ESCALATE",
    text="Your billing practices are unacceptable and I am contacting my lawyer.",
    account_tier="pro",
)
CANCEL_TICKET = Ticket(
    id="T-CANCEL",
    text="Please cancel the subscription on ACC-2041 at the end of the term.",
    account_tier="pro",
)
