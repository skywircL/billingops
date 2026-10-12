"""Spec v1.2: refund-policy questions, out-of-scope requests, and customer messages while a case is open."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from billingops.llm.base import ModelError
from billingops.llm.fake import ScriptedModel
from billingops.runtime import StateConflict

from conftest import approve, run_case

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc).isoformat()
MODES = ["fixed", "dynamic"]


def _kinds(rt, case_id: str) -> list[str]:
    return [e["kind"] for e in rt.store.events(case_id)]


def _conclusion(view) -> str:
    return (view["case"]["conclusion"] or {}).get("type")


# -- 4.5 refund-policy questions ---------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_refund_policy_question_is_answered_from_the_policy_record(runtime_factory, billing, mode) -> None:
    rt = runtime_factory(mode=mode)
    view = run_case(rt, "你们的退款政策是什么？")
    assert view["case"]["state"] == "closed" and _conclusion(view) == "explain_refund_policy"
    assert [(e["tool"], e["args"].get("name")) for e in view["evidence"]] == [("get_policy", "refunds")]
    assert view["case"]["proposal"] is None and not view["clarifications"]
    assert billing.customer_refunds("C-1001") == []


@pytest.mark.parametrize("mode", MODES)
def test_a_hypothetical_duplicate_is_a_policy_question_not_a_claim(runtime_factory, billing, mode) -> None:
    rt = runtime_factory(mode=mode)
    view = run_case(rt, "如果被重复扣费，能全额退吗？")
    assert _conclusion(view) == "explain_refund_policy"
    assert not any(e["tool"] in ("list_invoices", "list_payments") for e in view["evidence"])


@pytest.mark.parametrize("mode", MODES)
def test_an_actual_charge_beats_a_policy_question(runtime_factory, billing, mode) -> None:
    rt = runtime_factory(mode=mode)
    view = run_case(rt, "我好像被扣了两次，你们是全额退吗？")
    # Spec 4.5: the claim is investigated as a duplicate (no ids given, so the desk asks for one first).
    assert view["case"]["state"] == "awaiting_user"


# -- 4.6 out-of-scope requests -------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_out_of_scope_request_is_explained_without_reads_or_questions(runtime_factory, billing, mode) -> None:
    rt = runtime_factory(mode=mode)
    view = run_case(rt, "我想把扣款的卡换成新的，卡号 4111 1111 1111 1111。")
    assert view["case"]["state"] == "closed" and _conclusion(view) == "explain_out_of_scope"
    assert view["evidence"] == [] and view["clarifications"] == []
    assert "4111" not in (view["case"]["final_message"] or "")


@pytest.mark.parametrize("mode", MODES)
def test_a_plan_change_is_out_of_scope_not_a_price_explanation(runtime_factory, billing, mode) -> None:
    rt = runtime_factory(mode=mode)
    view = run_case(rt, "请帮我把套餐从专业版降到基础版。")
    assert _conclusion(view) == "explain_out_of_scope"


@pytest.mark.parametrize("mode", MODES)
def test_out_of_scope_next_to_a_duplicate_claim_handles_the_claim(runtime_factory, billing, mode) -> None:
    rt = runtime_factory(mode=mode)
    view = run_case(rt, "我想换卡，另外发票 INV-2026-0142 被扣了两次。")
    assert view["case"]["state"] == "awaiting_approval"
    assert view["case"]["proposal"]["payment_id"] == "PAY-88213"


# -- 5.2 a message while the proposal waits for approval ----------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_withdrawal_while_awaiting_approval_closes_without_a_refund(runtime_factory, billing, mode) -> None:
    rt = runtime_factory(mode=mode)
    case = run_case(rt)["case"]
    assert case["state"] == "awaiting_approval"
    rt.submit_message(case["id"], text="不用退了，帮我撤销吧。", actor="cust-c1001")
    rt.drain()
    view = rt.case_view(case["id"])
    assert view["case"]["state"] == "closed" and _conclusion(view) == "refund_withdrawn"
    assert view["case"]["gate_token"] is None and view["execution"] is None
    assert view["invalidated_proposals"][-1]["reason"] == "withdrawn_by_customer"
    assert "撤销" in view["case"]["final_message"]
    with pytest.raises(StateConflict):  # no approval can apply to a withdrawn proposal
        rt.submit_decision(case["id"], decision="approve", approver="ops-dana", note="", proposal_version=case["proposal_version"], token=None)
    assert billing.payment_refunds("C-1001", "PAY-88213") == []


def test_an_approval_queued_before_the_withdrawal_is_processed_never_executes(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    runtime.submit_message(case["id"], text="I don't need the refund anymore, please withdraw it.", actor="cust-c1001")
    late = {"command_id": "cmd-late-approval", "token": case["gate_token"], "approver": "ops-dana", "decision": "approve", "note": "", "decided_at": NOW, "proposal_version": case["proposal_version"]}
    runtime.store.enqueue_task(case["id"], "decision", late)
    runtime.drain()
    assert runtime.store.get_case(case["id"])["state"] == "closed"
    assert billing.payment_refunds("C-1001", "PAY-88213") == []
    assert "refund_sending" not in _kinds(runtime, case["id"])


@pytest.mark.parametrize("mode", MODES)
def test_progress_question_keeps_the_same_proposal_and_approval_still_works(runtime_factory, billing, mode) -> None:
    rt = runtime_factory(mode=mode)
    case = run_case(rt)["case"]
    command_id = rt.submit_message(case["id"], text="我的退款到哪一步了？", actor="cust-c1001")
    rt.drain()
    after = rt.store.get_case(case["id"])
    assert after["state"] == "awaiting_approval"
    assert (after["proposal_version"], after["gate_token"]) == (case["proposal_version"], case["gate_token"])
    assert "等待人工审批" in (rt.reply_for(case["id"], command_id) or "")
    kinds = _kinds(rt, case["id"])
    assert kinds.count("approval_requested") == 1 and "approval_still_pending" in kinds
    assert "proposal_modification" not in kinds
    view = approve(rt, case["id"])
    assert view["execution"]["status"] == "done"
    assert len(billing.payment_refunds("C-1001", "PAY-88213")) == 1


def test_an_approval_queued_behind_a_progress_question_still_applies(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    runtime.submit_message(case["id"], text="When will it be approved?", actor="cust-c1001")
    decision = {"command_id": "cmd-approval", "token": case["gate_token"], "approver": "ops-dana", "decision": "approve", "note": "", "decided_at": NOW, "proposal_version": case["proposal_version"]}
    runtime.store.enqueue_task(case["id"], "decision", decision)
    runtime.drain()
    assert runtime.store.get_case(case["id"])["state"] == "closed"
    assert len(billing.payment_refunds("C-1001", "PAY-88213")) == 1


@pytest.mark.parametrize("mode", MODES)
def test_an_unclear_message_is_treated_as_new_information(runtime_factory, billing, mode) -> None:
    rt = runtime_factory(mode=mode)
    case = run_case(rt)["case"]
    rt.submit_message(case["id"], text="还有一件事，我当时是在手机上付的款。", actor="cust-c1001")
    rt.drain()
    view = rt.case_view(case["id"])
    assert view["case"]["proposal_version"] == case["proposal_version"] + 1
    assert view["invalidated_proposals"][0]["version"] == case["proposal_version"]
    assert "proposal_modification" in _kinds(rt, case["id"])


def test_identifiers_always_mean_new_information_even_next_to_withdraw_words(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    runtime.submit_message(case["id"], text="撤销？不是，我是想确认一下 PAY-88213 那笔。", actor="cust-c1001")
    runtime.drain()
    view = runtime.case_view(case["id"])
    assert view["case"]["state"] == "awaiting_approval"
    assert view["case"]["proposal_version"] == case["proposal_version"] + 1
    assert "refund_withdrawn" not in _kinds(runtime, case["id"])


class _FollowupFails(ScriptedModel):
    def generate(self, messages, tool_schemas, config):
        if config.tag == "followup":
            raise ModelError("classifier unavailable")
        return super().generate(messages, tool_schemas, config)


def test_a_classifier_failure_falls_back_to_new_information(runtime_factory, billing) -> None:
    rt = runtime_factory(model=_FollowupFails(), mode="fixed")
    case = run_case(rt)["case"]
    rt.submit_message(case["id"], text="我的退款到哪一步了？", actor="cust-c1001")
    rt.drain()
    events = rt.store.events(case["id"])
    assert any(e["kind"] == "followup_classified" and e["payload"]["reason"] == "model_error" and e["payload"]["kind"] == "new_info" for e in events)
    assert rt.store.get_case(case["id"])["proposal_version"] == case["proposal_version"] + 1


# -- 5.5 progress questions in the other open states -------------------------------------------------


def test_progress_question_while_escalated_or_uncertain_changes_nothing(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    runtime.submit_escalation(case["id"], operator="ops-dana", reason="needs a look")
    tasks_before = runtime.store.pending_tasks(case["id"])
    command_id = runtime.submit_message(case["id"], text="进度怎么样了？", actor="cust-c1001")
    assert "人工" in runtime.reply_for(case["id"], command_id)
    assert runtime.store.get_case(case["id"])["state"] == "escalated" and runtime.store.pending_tasks(case["id"]) == tasks_before

    runtime.store.set_state(case["id"], "uncertain")
    command_id = runtime.submit_message(case["id"], text="Has my refund gone through?", actor="cust-c1001")
    reply = runtime.reply_for(case["id"], command_id)
    assert "cannot say whether it was refunded" in reply
    assert runtime.store.get_case(case["id"])["state"] == "uncertain"


def test_progress_question_while_investigating_does_not_disturb_the_run(runtime, billing) -> None:
    case = runtime.submit_case(customer_id="C-1001", created_by="cust-c1001", text="I was charged twice for invoice INV-2026-0142 (49.00 yuan each). Please refund the duplicate.")
    command_id = runtime.submit_message(case["id"], text="有进展吗？", actor="cust-c1001")
    assert "正在核查" in runtime.reply_for(case["id"], command_id)
    assert [t["kind"] for t in runtime.store.pending_tasks(case["id"])] == ["start"]
    runtime.drain()
    assert runtime.store.get_case(case["id"])["state"] == "awaiting_approval"


def test_a_closed_case_takes_no_messages(runtime, billing) -> None:
    case = run_case(runtime)["case"]
    approve(runtime, case["id"])
    with pytest.raises(StateConflict):
        runtime.submit_message(case["id"], text="到哪一步了？", actor="cust-c1001")


@pytest.mark.parametrize("mode", MODES)
def test_withdrawing_a_completed_refund_is_explained_not_reversed(runtime_factory, billing, mode) -> None:
    rt = runtime_factory(mode=mode)
    first = run_case(rt)["case"]
    approve(rt, first["id"])
    assert len(billing.payment_refunds("C-1001", "PAY-88213")) == 1
    view = run_case(rt, "付款 PAY-88213 的退款我不要了，帮我撤销。")
    assert _conclusion(view) == "explain_refund_status"
    assert len(billing.payment_refunds("C-1001", "PAY-88213")) == 1  # nothing reversed, nothing sent again
