"""The prompt clauses two live regressions were traced to, pinned so an edit cannot drop them silently.

Round 2 aborted twice because a prompt edit changed behaviour in ways no offline test could see: the dynamic
group escalated ordinary refund-status questions, and later answered a duplicate-charge request as a status
question once the payment turned out to be fully refunded. Each clause below is the wording that fixed one of
those, with the run that caught it.
"""

from __future__ import annotations

import pytest

from billingops.investigation.prompts import EXTRACT_SYSTEM, INVESTIGATE_SYSTEM
from billingops.tools import EXTRACT_TOOL_SCHEMA


@pytest.mark.parametrize(
    "clause",
    [
        # runs/aborted/v4r2b-canary-v1-prompt-regression: escalation had been open-ended.
        "Escalate in exactly three situations, and in no others",
        "Never escalate a case whose records you have read",
        # spec 4.3: the customer is owed the recorded facts even when the answer is an escalation.
        "only after you have read the subscription history and the pricing policy",
        # runs/aborted/v4r2b-canary-v1-second-prompt-regression: a duplicate stays a duplicate.
        "already been refunded in full",
        "asking how an existing refund is progressing",
        # spec 6: an outage is not a wrong identifier.
        "A read that returns 'not found' is an answer, not an outage",
        # spec 4.1 rule 3.
        "the payment is not on that invoice",
    ],
)
def test_investigation_prompt_keeps_its_normative_clauses(clause: str) -> None:
    assert clause in INVESTIGATE_SYSTEM


def test_extraction_scopes_the_subscription_refund_flag() -> None:
    """Chasing a promised refund must not be extracted as a subscription refund request (first regression).

    The binding text is the tool description, which the model always sees next to the field. The system prompt
    carries the same rule in a shorter form; the explicit "status question, not this" sentence is queued for the
    next behaviour version rather than edited into a prompt a running batch is already measuring.
    """
    field = EXTRACT_TOOL_SCHEMA["input_schema"]["properties"]["requests_price_adjustment"]["description"]
    assert "SUBSCRIPTION itself" in field and "status question, not this" in field
    assert "requests_other_account" in EXTRACT_TOOL_SCHEMA["input_schema"]["properties"]
    assert "Set requests_other_account" in EXTRACT_SYSTEM
