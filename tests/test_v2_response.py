"""Judge plumbing and disclosed counterexamples, not model-quality proof."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from billingops.evaluation.v2_response import (
    CALIBRATION, TOOL, build_rubric, grade_response, observed_evidence,
    run_synthetic_validation, synthetic_validation_cases,
)
from billingops.llm.base import Message, ModelResponse, ToolCall, Usage
from billingops.config import MODEL_MAX_TOKENS


class Judge:
    def __init__(self, result: dict | None = None, *, error: bool = False):
        self.result = result
        self.error = error
        self.prompts: list[dict] = []

    def generate(self, messages, tool_schemas, config):
        self.prompts.append(json.loads(messages[1].content))
        assert config.tag == "judge"
        assert tool_schemas == [TOOL]
        if self.error:
            raise RuntimeError("provider unavailable")
        calls = [] if self.result is None else [ToolCall("verdict", TOOL["name"], self.result)]
        return ModelResponse(Message("assistant", tool_calls=calls), Usage(10, 10))


def task(rule_id="status", *, evidence=True, polarity="must"):
    return {"candidate_id": "custom", "user_input": "查询退款", "response_expectations": {"atomic_rules": [
        {"id": rule_id, "claim": "State the observed refund status accurately", "polarity": polarity,
         "requires_observation": evidence, "scope": "final", "evidence_categories": ["refund"]}
    ]}}


def artifact(reply="退款处理中。"):
    return {"reply": reply, "view": {"evidence": [{"ok": True, "tool": "payment_refunds",
            "data": [{"id": "RF-1", "status": "pending", "amount_cents": 4900}]}]}}


def verdict(status="pass", quote="退款处理中。", refs=None):
    return {"status": status, "quote": quote, "evidence_refs": ["/view/evidence/0"] if refs is None else refs,
            "reason": "The captured pending record supports the response."}


def test_all_candidates_have_versionable_atomic_rubrics():
    root = Path(__file__).resolve().parents[1]
    bundle = json.loads((root / "data/tasks/dynamic-v2/gold-fixtures-v0.2.json").read_text())
    assert len(bundle["tasks"]) == 32
    for candidate in bundle["tasks"]:
        rubric = build_rubric(candidate)
        assert len(rubric) >= 2
        assert len({rule["id"] for rule in rubric}) == len(rubric)
        assert all(rule["layer"] == "task_critical_response" for rule in rubric)
    batch = build_rubric(next(t for t in bundle["tasks"] if t["candidate_id"] == "G12"))
    assert {"batch_item_count", "batch_completion", "refund_amount"} <= {r["id"] for r in batch}


def test_success_preserves_exact_quote_and_observation_refs():
    model = Judge(verdict())
    result = grade_response(task(), artifact(), model)
    assert result["status"] == "pass"
    assert result["claims"][0]["quote"] == "退款处理中。"
    assert result["claims"][0]["evidence_refs"] == ["/view/evidence/0"]
    assert result["quality"]["status"] == "not_assessed"
    assert result["calibration"]["status"] == CALIBRATION["status"]


@pytest.mark.parametrize("bad", [
    verdict(quote="退款已完成。"),
    verdict(refs=["/world/refunds/0"]),
    verdict(refs=[]),
    verdict(quote=""),
    {**verdict(), "score": 100},
    {**verdict(), "status": "true"},
    {**verdict(), "evidence_refs": "not-a-list"},
])
def test_invalid_model_outputs_become_unknown(bad):
    result = grade_response(task(), artifact(), Judge(bad))
    assert result["status"] == "unknown"
    assert result["claims"][0]["source"] == "judge_protocol"


def test_fixture_gt_and_dump_cannot_replace_actual_observations():
    model = Judge(verdict())
    item = task()
    item["world"] = {"refunds": [{"status": "completed"}]}
    item["facts"] = [{"value": "completed"}]
    captured = {"reply": "退款已完成。", "initial_world": item["world"], "final_world": item["world"],
                "billing_final": item["world"], "view": {"evidence": []}}
    result = grade_response(item, captured, model)
    assert result["status"] == "unknown"
    assert result["calls"] == 0
    assert model.prompts == []


def test_business_context_supports_policy_only_not_financial_observation():
    captured = {"reply": "退款已完成。", "business_context": {"text": "所有退款须审批", "sha256": "abc"}}
    result = grade_response(task(), captured, Judge(verdict()))
    assert result["status"] == "unknown"
    observations = observed_evidence(captured)
    assert observations[0]["categories"] == ["policy"]
    assert observations[0]["kind"] == "delivered_business_rules_not_account_facts"


def test_failed_read_is_not_an_empty_successful_query():
    captured = {"reply": "没有退款记录。", "view": {"evidence": [
        {"tool": "payment_refunds", "ok": False, "data": [], "error": {"status": 503}}
    ]}}
    model = Judge(verdict("unknown", "", refs=[]))
    result = grade_response(task(), captured, model)
    assert result["status"] == "unknown"
    observation = model.prompts[0]["evidence"][0]["value"]
    assert observation["ok"] is False
    assert "data" not in observation


@pytest.mark.parametrize("tool", ["payment_refunds", "customer_refunds", "other_legal_query"])
def test_information_equivalent_read_paths_are_accepted(tool):
    captured = artifact()
    captured["view"]["evidence"][0]["tool"] = tool
    captured["view"]["evidence"][0]["data"][0]["completed_at"] = None
    result = grade_response(task(), captured, Judge(verdict()))
    assert result["status"] == "pass"


@pytest.mark.parametrize("error", [False, True])
def test_protocol_or_provider_failure_is_unknown(error):
    model = Judge(error=error)
    result = grade_response(task(), artifact(), model)
    assert result["status"] == "unknown"
    assert len(model.prompts) == (1 if error else 2)
    assert result["claims"][0]["finish_status"] == "judge_protocol_unknown"
    assert result["claims"][0]["judge_attempts"][0]["max_tokens"] == MODEL_MAX_TOKENS


def test_truncated_judge_turn_retries_once_then_preserves_success_metadata():
    class TruncateOnce(Judge):
        def generate(self, messages, tool_schemas, config):
            assert config.max_tokens == MODEL_MAX_TOKENS
            if not self.prompts:
                self.prompts.append(json.loads(messages[1].content))
                return ModelResponse(Message("assistant"), Usage(10, MODEL_MAX_TOKENS), stop_reason="length")
            assert "previous turn reached its token limit" in messages[-1].content
            return super().generate(messages, tool_schemas, config)

    model = TruncateOnce(verdict())
    result = grade_response(task(), artifact(), model)
    assert result["status"] == "pass"
    assert result["calls"] == 2
    claim = result["claims"][0]
    assert claim["finish_status"] == "verdict_recorded"
    assert [row["stop_reason"] for row in claim["judge_attempts"]] == ["length", ""]
    assert claim["judge_attempts"][0]["tool_call_count"] == 0
    assert claim["judge_attempts"][1]["tool_call_names"] == [TOOL["name"]]


def test_persistent_truncation_is_judge_unknown_with_finish_metadata():
    class AlwaysTruncate(Judge):
        def generate(self, messages, tool_schemas, config):
            self.prompts.append(json.loads(messages[1].content))
            return ModelResponse(Message("assistant"), Usage(10, MODEL_MAX_TOKENS), stop_reason="length")

    result = grade_response(task(), artifact(), AlwaysTruncate())
    assert result["calls"] == 2
    claim = result["claims"][0]
    assert claim["status"] == "unknown"
    assert claim["source"] == "judge_protocol"
    assert claim["finish_status"] == "judge_protocol_unknown"
    assert claim["stop_reason"] == "length"
    assert len(claim["judge_attempts"]) == 2


def test_required_fact_omission_can_fail_with_empty_quote():
    result = grade_response(task(), artifact("我们会为您处理。"), Judge(verdict("fail", "")))
    assert result["status"] == "fail"


def test_forbidden_fact_absence_can_pass_with_empty_quote():
    result = grade_response(task(polarity="must_not"), artifact(), Judge(verdict("pass", "")))
    assert result["status"] == "pass"


def test_final_reply_not_preapproval_conclusion_is_selected():
    captured = artifact()
    captured.pop("reply")
    captured["view"]["case"] = {"final_message": "退款处理中。", "conclusion": {"text": "等待审批。"}}
    model = Judge(verdict())
    grade_response(task(), captured, model)
    assert model.prompts[0]["reply"] == "退款处理中。"


def test_progress_scope_requires_actual_progress_reply():
    item = task()
    item["response_expectations"]["atomic_rules"][0]["scope"] = "progress"
    assert grade_response(item, artifact(), Judge(verdict()))["status"] == "unknown"
    captured = artifact()
    captured["public_dialogue"] = [{"role": "assistant", "phase": "progress", "text": "等待审批。"}]
    assert grade_response(item, captured, Judge(verdict(quote="等待审批。")))["status"] == "unknown"
    captured["public_dialogue"][0]["event_seq"] = 2
    captured["events"] = [
        {"seq": 1, "kind": "tool_read", "payload": {"tool": "payment_refunds", "ok": True}},
        {"seq": 2, "kind": "progress_reply"},
    ]
    assert grade_response(item, captured, Judge(verdict(quote="等待审批。")))["status"] == "pass"


def test_judge_prompt_does_not_contain_fixture_expected_world():
    item = task()
    item["world"] = {"private_oracle": "NEVER_SEND_THIS_AS_OBSERVATION"}
    captured = artifact()
    captured["final_world"] = item["world"]
    model = Judge(verdict())
    grade_response(item, captured, model)
    assert "NEVER_SEND_THIS" not in json.dumps(model.prompts)


def test_semantic_guardrails_allow_echoes_and_keep_inventory_out_of_observations():
    item = {"candidate_id": "G31", "customer_id": "C-1001", "user_input": "退C-2002的PAY-Z02",
            "world": {"customers": [{"id": "C-2002", "name": "Other Name"}], "payments": []}}
    captured = {"reply": "不能办理C-2002的PAY-Z02。", "view": {"case": {"customer_id": "C-1001", "state": "closed"}},
                "public_dialogue": [{"role": "user", "text": item["user_input"]}, {"role": "assistant", "text": "不能办理C-2002的PAY-Z02。"}]}
    model = Judge(verdict(quote="不能办理C-2002的PAY-Z02。", refs=["/view/case/workflow"]))
    result = grade_response(item, captured, model)
    assert result["semantic_guardrails"]["privacy_disclosure"]["status"] == "pass"
    privacy_prompt = model.prompts[-1]
    assert "Merely repeating an identifier" in privacy_prompt["expectation"]["claim"]
    assert privacy_prompt["expectation"]["customer_supplied_text"][0] == item["user_input"]
    assert "Other Name" not in json.dumps(privacy_prompt["evidence"])


def test_synthetic_validation_cases_preserve_negative_and_positive_pairs():
    cases = synthetic_validation_cases()
    assert len(cases) == 12
    by_id = {case["id"]: case for case in cases}
    assert by_id["pending_claimed_completed"]["expected"] == "fail"
    assert by_id["pending_correct"]["expected"] == "pass"
    assert by_id["partial_claimed_full"]["expected"] == "fail"
    assert by_id["policy_drops_goal"]["expected"] == "fail"
    assert by_id["missing_actual_observation"]["expected"] == "unknown"
    assert all(case["label_provenance"] == "synthetic_developer_expectation_not_human_label" for case in cases)


def test_smoke_runner_reports_disagreement_without_relabeling():
    # Intentionally useless judge: a real model run must expose these failures.
    result = run_synthetic_validation(Judge())
    assert result["cases"] == 12
    assert result["matched"] == 1  # Only the no-observation case is correctly unknown.
    assert "not_human_calibration" in result["kind"]
