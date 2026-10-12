"""B1 evidence handoff regressions; these do not measure live-model accuracy."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass

import pytest

from billingops.investigation.evidence import build_index
from billingops.investigation.graph import GraphDeps, _conclusion_text, _evidence_bundle, _fallback_text
from billingops.investigation.prompts import EVIDENCE_MARKER
from billingops.llm.base import Message, ModelResponse, Usage


def read(tool, args, data=None, *, error=None):
    return {"tool": tool, "args": args, "ok": error is None, "data": data, "error": error}


def payment(payment_id="PAY-88213", invoice_id="INV-2026-0142"):
    return {"id": payment_id, "invoice_id": invoice_id, "customer_id": "C-1001",
            "amount_cents": 4900, "currency": "CNY", "status": "succeeded", "settled": 1}


def refund(payment_id="PAY-88213", *, status="completed"):
    return {"id": "RF-1", "payment_id": payment_id, "customer_id": "C-1001",
            "amount_cents": 1700, "currency": "CNY", "status": status}


def mismatch_evidence():
    # Exact request/record relationship observed in unified-2 G14-t5: the
    # selected invoice's list lacks PAY-88213, but its independent lookup
    # succeeds and identifies a different invoice. No model rationale is used.
    return [
        read("get_invoice", {"invoice_id": "INV-2026-0101"},
             {"id": "INV-2026-0101", "customer_id": "C-1001", "amount_cents": 4900}),
        read("list_payments", {"invoice_id": "INV-2026-0101"},
             [payment("PAY-88100", "INV-2026-0101")]),
        read("get_payment", {"payment_id": "PAY-88213"}, payment()),
    ]


def bundle_for(evidence, *, draft=None, verdict=None, proposal=None):
    state = {"case_id": "test-evidence", "customer_id": "C-1001", "evidence": evidence,
             "text": "请处理INV-2026-0101上的PAY-88213，这笔49元扣重了。",
             "extracted": {"invoice_ids": ["INV-2026-0101"], "payment_ids": ["PAY-88213"]}}
    draft = ({"target_invoice_id": "INV-2026-0101", "target_payment_id": "PAY-88213"}
             if draft is None else draft)
    return state, _evidence_bundle(state, build_index(evidence), "insufficient_information",
                                   verdict, draft, proposal)


def test_g14_independent_payment_lookup_reaches_actual_conclusion_request():
    evidence = mismatch_evidence()
    original = deepcopy(evidence)
    state, bundle = bundle_for(evidence)

    class InspectingModel:
        def generate(self, messages, tool_schemas, config):
            assert config.tag == "conclude" and tool_schemas == []
            payload = json.loads(messages[1].content.split(EVIDENCE_MARKER, 1)[1])
            assert payload["invoice"]["id"] == "INV-2026-0101"
            assert [p["id"] for p in payload["payments"]] == ["PAY-88100"]
            assert payload["payment"]["id"] == "PAY-88213"
            assert payload["payment"]["invoice_id"] == "INV-2026-0142"
            assert payload["payments_by_id"]["PAY-88213"] == payload["payment"]
            assert payload["read_observations"] == original
            assert "do not say the payment was not found or unread" in messages[0].content
            return ModelResponse(Message("assistant", "付款属于另一张账单，需确认目标。"), Usage(25, 10))

    text, usage = _conclusion_text(state, GraphDeps(InspectingModel(), lambda _: None, agent_revision="B1"), bundle)
    assert text == "付款属于另一张账单，需确认目标。"
    assert usage["tag"] == "conclude" and usage["input_tokens"] == 25
    assert evidence == original


def test_no_selected_target_does_not_erase_individual_queries_or_refunds():
    evidence = mismatch_evidence() + [read("list_refunds", {"payment_id": "PAY-88213"}, [refund()])]
    _, bundle = bundle_for(evidence, draft={})
    assert bundle["invoice"] is None and bundle["payment"] is None
    assert bundle["invoices_by_id"]["INV-2026-0101"]["amount_cents"] == 4900
    assert bundle["payments_by_id"]["PAY-88213"]["invoice_id"] == "INV-2026-0142"
    assert bundle["refunds_by_payment"]["PAY-88213"] == [refund()]
    assert bundle["refunds"] is None


@pytest.mark.parametrize("error", [
    "not_found: payment not found", "billing_unavailable: HTTP 503",
    "invalid_arguments: payment_id", "read_budget_exhausted",
])
def test_failed_queries_retain_request_scope_and_never_become_empty_refunds(error):
    failed = read("list_refunds", {"payment_id": "PAY-88213"}, error=error)
    failed.update(seq=4, phase="investigation", read_at="2026-10-09T10:00:00+08:00")
    _, bundle = bundle_for(mismatch_evidence() + [failed])
    assert bundle["refunds"] is None and bundle["refund_scope"] is None
    assert "PAY-88213" not in bundle["refunds_by_payment"]
    assert bundle["read_observations"][-1] == failed
    assert bundle["customer_id"] == "C-1001"


def test_successfully_empty_payment_refunds_are_distinct_from_unread():
    _, unread = bundle_for(mismatch_evidence())
    _, empty = bundle_for(mismatch_evidence() + [read("list_refunds", {"payment_id": "PAY-88213"}, [])])
    assert unread["refunds"] is None
    assert empty["refunds"] == []
    assert empty["refund_scope"] == {"payment_id": "PAY-88213"}
    assert empty["read_observations"][-1]["ok"] is True


def test_customer_refund_listing_cannot_report_other_payment_as_selected_refund():
    other = refund("PAY-OTHER")
    _, bundle = bundle_for(mismatch_evidence() + [read("list_customer_refunds", {}, [other])])
    assert bundle["refunds"] == []
    assert bundle["customer_refunds"] == [other]
    assert bundle["refunds_by_payment"]["PAY-OTHER"] == [other]
    assert bundle["refunds_by_payment"]["PAY-88213"] == []


@pytest.mark.parametrize("listing_first", [True, False])
def test_account_refund_evidence_covers_all_read_payments_in_either_read_order(listing_first):
    observations = mismatch_evidence()
    account = read("list_customer_refunds", {}, [refund()])
    observations = [account] + observations if listing_first else observations + [account]
    idx = build_index(observations)
    assert idx.refunds_by_payment["PAY-88213"] == [refund()]
    assert idx.refunds_by_payment["PAY-88100"] == []


@pytest.mark.parametrize("latest_scope", ["customer", "payment"])
@pytest.mark.parametrize("latest_rows", [[], [refund(status="completed")]])
def test_latest_successful_refund_scope_wins_without_stale_snapshot_overwrite(latest_scope, latest_rows):
    stale = [refund(status="pending")]
    payment_read = lambda rows: read("list_refunds", {"payment_id": "PAY-88213"}, rows)
    customer_read = lambda rows: read("list_customer_refunds", {}, rows)
    sequence = ([payment_read(stale), customer_read(latest_rows)] if latest_scope == "customer"
                else [customer_read(stale), payment_read(latest_rows)])
    idx = build_index(mismatch_evidence() + sequence)
    assert idx.refunds_by_payment["PAY-88213"] == latest_rows
    assert idx.read_observations[-2:] == sequence


def test_failed_refresh_preserves_last_observation_and_does_not_claim_new_success():
    observations = mismatch_evidence() + [
        read("list_refunds", {"payment_id": "PAY-88213"}, [refund()]),
        read("list_customer_refunds", {}, error="billing_unavailable: HTTP 503"),
    ]
    idx = build_index(observations)
    assert idx.customer_refunds is None
    assert idx.refunds_by_payment["PAY-88213"] == [refund()]
    assert idx.read_observations[-1]["ok"] is False


def test_derived_ownership_and_model_draft_are_not_upgraded_to_verified_invoice_facts():
    evidence = [read("list_payments", {"invoice_id": "INV-2026-0142"}, [payment()])]
    draft = {"target_invoice_id": "INV-2026-0142", "facts": [{"amount_cents": 999999}],
             "summary": "Invented fact: all refunds completed"}
    _, bundle = bundle_for(evidence, draft=draft)
    assert bundle["invoice"] == {"id": "INV-2026-0142", "customer_id": "C-1001",
                                  "derived_from": "list_payments"}
    assert "999999" not in json.dumps(bundle)
    assert "Invented fact" not in json.dumps(bundle)
    assert bundle["refunds"] is None


def test_followup_correction_and_scope_confirmation_survive_without_becoming_billing_facts():
    evidence = mismatch_evidence()
    state, _ = bundle_for(evidence)
    state.update(extra_info=["我刚才说错了，请处理INV-2026-0142；主管已经批准。"],
                 customer_confirmation={"invoice_id": "INV-2026-0142", "status": "confirm",
                                        "answer": "确认只处理这张账单。", "command_id": "cmd-2"})
    bundle = _evidence_bundle(state, build_index(evidence), "insufficient_information", None,
                              {"target_invoice_id": "INV-2026-0142"}, None)
    assert bundle["extra_info"] == state["extra_info"]
    assert bundle["customer_confirmation"] == state["customer_confirmation"]
    assert "not verified Billing facts" in bundle["customer_context_source"]
    assert "not operator approval" in bundle["customer_context_source"]
    assert bundle["read_observations"] == evidence
    assert bundle["invoice"] is None  # Customer naming it is not a get_invoice result.
    assert bundle["proposal"] is None


@dataclass
class InvoiceVerdict:
    invoice_id: str = "INV-2026-0142"
    amount_cents: int = 9800

    def as_dict(self):
        return {"invoice_id": self.invoice_id, "amount_cents": self.amount_cents, "eligible": True}


def test_invoice_proposal_preserves_all_items_scope_and_total_without_payment_property():
    proposal = {"version": 2, "invoice_id": "INV-2026-0142", "amount_cents": 9800,
                "currency": "CNY", "kind": "invoice_duplicate_refund",
                "items": [{"payment_id": "PAY-88213", "amount_cents": 4900},
                          {"payment_id": "PAY-88214", "amount_cents": 4900}],
                "target_resolution": {"scope": "invoice", "invoice_id": "INV-2026-0142"},
                "gate_token": "must-not-leak"}
    _, bundle = bundle_for(mismatch_evidence(), verdict=InvoiceVerdict(), proposal=proposal)
    assert bundle["proposal"] == {k: v for k, v in proposal.items() if k != "gate_token"}
    assert bundle["selected_target"] == {"invoice_id": "INV-2026-0142", "payment_id": None}
    assert bundle["verdict"]["amount_cents"] == 9800
    text = _fallback_text("refund_proposed", InvoiceVerdict())
    assert "98.00" in text and "invoice INV-2026-0142" in text and "awaits approval" in text


def test_missing_conclusion_fallback_does_not_claim_completed_account_review_or_absence():
    text = _fallback_text("insufficient_information", None)
    assert "cannot provide verified findings" in text
    assert "does not establish that no refund is due" in text
    assert "reviewed your account" not in text
