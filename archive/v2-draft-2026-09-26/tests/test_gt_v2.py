"""Ground truth v2: the calculator, the gold seeds, and the validator's ability to say no."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from billingops.evaluation import gt_v2
from billingops.subscriptions import policy
from billingops.subscriptions.capability_map import LEAVES

ROOT = Path(__file__).resolve().parents[1]


def test_calculator_reproduces_the_business_plan_examples() -> None:
    # Plan §3.2: Pro -> Max 5x with half the period and window left, 900 units used.
    q = policy.upgrade_quote(current_plan="pro", target_plan="max_5x", period_start="2026-09-01T00:00:00Z", period_end="2026-10-01T00:00:00Z",
                             window_start="2026-09-01T00:00:00Z", window_end="2026-10-01T00:00:00Z", granted=1000, used=900, now="2026-09-16T00:00:00Z")
    assert (q["charge_cents"], q["added_units"], q["granted_after"], q["available_after"]) == (4000, 2000, 3000, 2100)
    # Plan §3.3 example 1: Pro monthly -> annual with half a $20 month left.
    c = policy.cross_cycle_quote(period_start="2026-09-01T00:00:00Z", period_end="2026-10-01T00:00:00Z", period_settled_cents=2000,
                                 target_plan="pro", target_cycle="annual", now="2026-09-16T00:00:00Z")
    assert (c["credit_applied_cents"], c["cash_due_cents"], c["credit_left_cents"]) == (1000, 19000, 0)
    # Plan §3.3 example 2: $150 of annual value against a $100 Max 5x month.
    c = policy.cross_cycle_quote(period_start="2026-01-01T00:00:00Z", period_end="2027-01-01T00:00:00Z", period_settled_cents=20000,
                                 target_plan="max_5x", target_cycle="monthly", now="2026-04-02T06:00:00Z")
    assert (c["unfulfilled_value_cents"], c["cash_due_cents"], c["credit_left_cents"]) == (15000, 0, 5000)


def test_time_and_window_boundaries() -> None:
    assert policy.iso(policy.add_months("2026-01-31T00:00:00Z", 1)) == "2026-02-28T00:00:00Z"
    assert policy.iso(policy.add_months("2028-02-29T00:00:00Z", 12)) == "2029-02-28T00:00:00Z"
    assert policy.within_refund_window("2026-09-14T00:00:00Z", "2026-09-21T00:00:00Z")          # exactly 168 h: inside
    assert not policy.within_refund_window("2026-09-14T00:00:00Z", "2026-09-21T00:00:01Z")
    assert policy.floor_units(policy.remaining_ratio("2026-09-16T00:00:01Z", "2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z") * 4000) == 1999


def test_gold_v2_is_valid_and_agrees_with_the_calculator() -> None:
    report = gt_v2.validate(gt_v2.build_seeds())
    assert report["ok"], report["errors"]
    assert report["tasks"] == 39 and report["calc_checks"] >= 33


def test_gold_v2_file_is_the_builder_output() -> None:
    on_disk = json.loads(gt_v2.GOLD_V2_PATH.read_text(encoding="utf-8"))
    assert on_disk == json.loads(json.dumps(gt_v2.build(), ensure_ascii=False)), "run: python -m billingops.evaluation gt-v2 build"


def _seed(seed_id: str) -> dict:
    return copy.deepcopy(next(t for t in gt_v2.build_seeds() if t["seed_id"] == seed_id))


def _errors(t: dict) -> list[str]:
    return gt_v2.validate_seed(t) + gt_v2.recompute_seed(t)


def _wrong_charge(t: dict) -> None:
    inv = t["expected"]["state"]["new_invoices"][0]
    inv["amount_cents"] = inv["cash_due_cents"] = 4001


def _no_confirmation(t: dict) -> None:
    t["reference_path"] = [s for s in t["reference_path"] if s["tool"] != "ask_customer"]


def _original_payment(t: dict) -> None:
    t["expected"]["state"]["new_refund_requests"][0]["payment_id"] = "PAY-1001-0914A"


def _silent_replacement(t: dict) -> None:
    t["reference_path"][2]["args"]["question"] = "确认取消吗？"


def _new_key_retry(t: dict) -> None:
    t["reference_path"].insert(5, {"tool": "commit_subscription_change", "args": {"quote_id": "Q1", "idempotency_key": "another"}, "result": "succeeded"})


def _cash_credit(t: dict) -> None:
    t["expected"]["state"]["credit_balance_cents"] = 1000


def _card_in_request(t: dict) -> None:
    t["reference_path"][4]["args"]["note"] = "4111 1111 1111 1111"


def _commit_on_quote_only(t: dict) -> None:
    t["reference_path"].insert(2, {"tool": "commit_subscription_change", "args": {"quote_id": "Q1", "idempotency_key": "k"}, "result": "succeeded"})


def _reuse_expired_confirmation(t: dict) -> None:
    # Commit the recalculated quote on the strength of the answer given to the expired one.
    asks = [i for i, x in enumerate(t["reference_path"]) if x["tool"] == "ask_customer"]
    del t["reference_path"][asks[-1]]


def _skip_confirmation_on_blanket_consent(t: dict) -> None:
    t["reference_path"] = [x for x in t["reference_path"] if x["tool"] != "ask_customer"]


def _world_invoice_off_by_a_cent(t: dict) -> None:
    inv = next(i for i in t["world"]["invoices"] if i["id"] == "INV-1001-0915")
    inv["amount_cents"] = inv["cash_due_cents"] = 7734


@pytest.mark.parametrize("seed_id,mutate,expect", [
    ("SUB-02", _wrong_charge, "computed charge"),
    ("SUB-02", _no_confirmation, "without a confirmation"),
    ("REF-02", _original_payment, "extra payment"),
    ("SUB-09", _silent_replacement, "replaced arrangement"),
    ("ERR-01", _new_key_retry, "new idempotency key"),
    ("SUB-07", _cash_credit, "credit balance"),
    ("SEC-02", _card_in_request, "card-like number"),
    ("SUB-01", _commit_on_quote_only, "forbidden commit_subscription_change"),
    ("SUB-18", _reuse_expired_confirmation, "without a confirmation"),
    ("SUB-17", _skip_confirmation_on_blanket_consent, "without a confirmation"),
    ("REF-13", _world_invoice_off_by_a_cent, "world invoice"),
])
def test_validator_rejects_a_broken_seed(seed_id, mutate, expect) -> None:
    t = _seed(seed_id)
    assert not _errors(t)
    mutate(t)
    assert any(expect in e for e in _errors(t)), _errors(t)


def test_capability_map_doc_and_code_list_the_same_leaves() -> None:
    doc = (ROOT / "docs" / "CAPABILITY_MAP_v2.md").read_text(encoding="utf-8")
    in_doc = set(re.findall(r"\|\s*([QCRFESB]-\d{2})\s*\|", doc))
    assert in_doc == set(LEAVES), (sorted(set(LEAVES) - in_doc), sorted(in_doc - set(LEAVES)))


def test_every_seed_is_listed_in_the_ground_truth_doc() -> None:
    doc = (ROOT / "docs" / "GROUND_TRUTH_v2.md").read_text(encoding="utf-8")
    for t in gt_v2.build_seeds():
        assert f"| {t['seed_id']} |" in doc, t["seed_id"]
