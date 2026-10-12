"""The harness is the product, so the harness gets tested like one.

Each measurement is checked against a rigged model whose mistakes are known in
advance, so the report's numbers can be asserted exactly rather than eyeballed.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from langgraph_production.evaluation.fixtures import (
    TicketFixture,
    check_consistency,
    load_fixtures,
    route_counts,
)
from langgraph_production.evaluation.interrupts import evaluate_interrupts
from langgraph_production.evaluation.report import (
    EvalReport,
    Thresholds,
    load_thresholds,
    run_evaluation,
)
from langgraph_production.evaluation.resume import (
    BoundaryResult,
    ResumeReport,
    ScenarioResult,
    evaluate_resume,
    select_scenarios,
)
from langgraph_production.evaluation.routing import evaluate_routing
from langgraph_production.llm.base import LLMRequest
from langgraph_production.llm.offline import OfflineSupportLLM
from langgraph_production.tools import ToolRegistry


def fixture(**overrides: object) -> TicketFixture:
    payload: dict[str, object] = {
        "id": "F-1",
        "text": "How do I export a dashboard?",
        "account_tier": "pro",
        "expected_route": "answer",
        "expected_tool": None,
        "must_interrupt": False,
        "note": "docs",
    }
    payload.update(overrides)
    return TicketFixture.model_validate(payload)


class AlwaysAnswer:
    def __call__(self, request: LLMRequest) -> str:
        if request.tag == "compose":
            return "handled"
        return json.dumps({"route": "answer", "confidence": 0.9, "rationale": "always"})


class AlwaysEscalate:
    def __call__(self, request: LLMRequest) -> str:
        if request.tag == "compose":
            return "handled"
        return json.dumps({"route": "escalate", "confidence": 0.9, "rationale": "always"})


# -- fixture file -------------------------------------------------------


def test_the_shipped_fixtures_load() -> None:
    assert len(load_fixtures()) >= 40


def test_the_shipped_fixtures_are_self_consistent() -> None:
    assert check_consistency(load_fixtures()) == ()


def test_the_shipped_fixtures_cover_every_branch() -> None:
    counts = route_counts(load_fixtures())
    assert min(counts.values()) >= 5


def test_the_shipped_fixtures_cover_every_tool() -> None:
    named = {row.expected_tool for row in load_fixtures() if row.expected_tool}
    assert named == set(ToolRegistry().names)


def test_a_fixture_that_contradicts_the_policy_is_caught() -> None:
    problems = check_consistency([fixture(expected_route="escalate", must_interrupt=False)])
    assert problems and "contradicts" in problems[0]


def test_an_escalation_that_names_a_tool_is_caught() -> None:
    problems = check_consistency(
        [fixture(expected_route="escalate", expected_tool="issue_refund", must_interrupt=True)]
    )
    assert any("cannot name a tool" in problem for problem in problems)


def test_an_unknown_tool_is_caught() -> None:
    problems = check_consistency(
        [fixture(expected_route="tool", expected_tool="wire_money", must_interrupt=True)]
    )
    assert any("unknown tool" in problem for problem in problems)


def test_duplicate_ids_are_caught() -> None:
    assert any("duplicate" in problem for problem in check_consistency([fixture(), fixture()]))


def test_a_bad_line_names_its_line_number(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "ok"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"bad\.jsonl:1"):
        load_fixtures(path)


def test_an_empty_file_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no fixtures"):
        load_fixtures(path)


# -- routing ------------------------------------------------------------


def test_routing_scores_a_perfect_run() -> None:
    cases = [fixture(id="a"), fixture(id="b")]
    report = evaluate_routing(cases, llm=AlwaysAnswer())
    assert report.accuracy == 1.0
    assert report.mistakes == ()


def test_routing_counts_every_miss() -> None:
    cases = [fixture(id="a"), fixture(id="b", expected_route="escalate", must_interrupt=True)]
    report = evaluate_routing(cases, llm=AlwaysAnswer())
    assert report.correct == 1
    assert report.accuracy == 0.5
    assert len(report.mistakes) == 1
    assert report.mistakes[0].kind == "escalate -> answer"


def test_the_confusion_matrix_places_the_error() -> None:
    cases = [fixture(id="a", expected_route="escalate", must_interrupt=True)]
    report = evaluate_routing(cases, llm=AlwaysAnswer())
    assert report.confusion["escalate"]["answer"] == 1
    assert report.confusion["escalate"]["escalate"] == 0


def test_precision_and_recall_are_computed_per_branch() -> None:
    cases = [
        fixture(id="a"),
        fixture(id="b"),
        fixture(id="c", expected_route="escalate", must_interrupt=True),
    ]
    report = evaluate_routing(cases, llm=AlwaysAnswer())
    answer = report.per_branch["answer"]
    assert answer.recall == 1.0
    assert answer.precision == pytest.approx(2 / 3)
    assert report.per_branch["escalate"].recall == 0.0


def test_an_empty_branch_scores_zero_without_dividing_by_zero() -> None:
    report = evaluate_routing([fixture(id="a")], llm=AlwaysAnswer())
    assert report.per_branch["tool"].f1 == 0.0


def test_choosing_the_wrong_tool_is_a_miss_even_on_the_right_branch() -> None:
    cases = [
        fixture(
            id="a",
            text="Order ORD-40021 has not arrived yet",
            expected_route="tool",
            expected_tool="lookup_invoice",
            expected_args={"invoice_ref": "INV-10032"},
            must_interrupt=False,
        )
    ]
    report = evaluate_routing(cases, llm=OfflineSupportLLM())
    assert report.tool_correct == 0
    assert report.arg_correct == 0
    assert report.mistakes[0].kind == "tool: wrong tool"


def test_the_right_tool_with_the_wrong_amount_is_a_miss() -> None:
    """The mistake a tool-name metric cannot see, and the one that pays out.

    The customer asks for the difference back on a ninety dollar charge. The
    stand-in reads the first figure in the message and proposes a ninety
    dollar refund. Right tool, right invoice, twice the money.
    """
    cases = [
        fixture(
            id="a",
            text="We were charged $90.00 on INV-10032 but only $45.00 was valid. Refund the difference.",
            expected_route="tool",
            expected_tool="issue_refund",
            expected_args={"invoice_ref": "INV-10032", "amount_cents": 4500},
            must_interrupt=True,
        )
    ]
    report = evaluate_routing(cases, llm=OfflineSupportLLM())
    assert report.tool_correct == 1, "the tool name is right, which is the whole problem"
    assert report.arg_correct == 0
    assert report.accuracy == 0.0
    assert report.mistakes[0].kind == "tool: wrong arguments"
    assert report.mistakes[0].actual_args == {"invoice_ref": "INV-10032", "amount_cents": 9000}


def test_argument_scoring_compares_parsed_values_not_raw_json() -> None:
    """4500 and "4500" are one amount, and the label must not call them two."""

    class StringAmount:
        def __call__(self, request: LLMRequest) -> str:
            return json.dumps(
                {
                    "route": "tool",
                    "tool": "issue_refund",
                    "args": {"invoice_ref": "INV-10032", "amount_cents": "4500"},
                    "confidence": 0.9,
                }
            )

    cases = [
        fixture(
            id="a",
            text="refund it",
            expected_route="tool",
            expected_tool="issue_refund",
            expected_args={"invoice_ref": "INV-10032", "amount_cents": 4500},
            must_interrupt=True,
        )
    ]
    report = evaluate_routing(cases, llm=StringAmount())
    assert report.arg_correct == 1
    assert report.mistakes == ()


def test_the_arguments_of_the_shipped_fixtures_are_scored() -> None:
    report = evaluate_routing(load_fixtures(), llm=OfflineSupportLLM())
    assert report.arg_correct <= report.tool_correct
    assert "counting the arguments" in report.render()


def test_a_tool_fixture_with_no_arguments_is_caught() -> None:
    problems = check_consistency(
        [
            fixture(
                expected_route="tool",
                expected_tool="issue_refund",
                expected_args=None,
                must_interrupt=True,
            )
        ]
    )
    assert any("no expected_args" in problem for problem in problems)


def test_arguments_that_do_not_fit_the_tool_are_caught() -> None:
    problems = check_consistency(
        [
            fixture(
                expected_route="tool",
                expected_tool="issue_refund",
                expected_args={"invoice_ref": "not an invoice", "amount_cents": 4500},
                must_interrupt=True,
            )
        ]
    )
    assert any("do not fit" in problem for problem in problems)


def test_an_answer_fixture_cannot_carry_arguments() -> None:
    problems = check_consistency([fixture(expected_args={"invoice_ref": "INV-10032"})])
    assert any("cannot name arguments" in problem for problem in problems)


def test_the_gate_fails_when_the_arguments_are_wrong() -> None:
    report = run_evaluation(
        fixtures=[
            fixture(
                id="a",
                text="We were charged $90.00 on INV-10032 but only $45.00 was valid. Refund the difference.",
                expected_route="tool",
                expected_tool="issue_refund",
                expected_args={"invoice_ref": "INV-10032", "amount_cents": 4500},
                must_interrupt=True,
            )
        ],
        thresholds=Thresholds(
            min_routing_accuracy=0.0,
            min_macro_f1=0.0,
            min_tool_accuracy=0.0,
            min_arg_accuracy=0.9,
        ),
        skip_resume=True,
    )
    assert report.passed is False
    assert any("tool call accuracy" in reason for reason in report.failures)


def test_untrusted_model_output_is_counted_by_reason() -> None:
    class Garbage:
        def __call__(self, request: LLMRequest) -> str:
            return "not json at all"

    report = evaluate_routing([fixture(id="a")], llm=Garbage())
    assert report.fallbacks == {"invalid_json": 1}


def test_the_routing_report_renders_its_matrix() -> None:
    rendered = evaluate_routing(load_fixtures(), llm=OfflineSupportLLM()).render()
    assert "confusion matrix" in rendered
    assert "per branch" in rendered


def test_the_routing_report_serialises() -> None:
    payload = evaluate_routing(load_fixtures(), llm=OfflineSupportLLM()).to_dict()
    assert set(payload) >= {"accuracy", "macro_f1", "confusion", "per_branch", "mistakes"}


# -- interrupts ---------------------------------------------------------


def test_interrupts_detects_a_missed_stop() -> None:
    cases = [fixture(id="a", expected_route="escalate", must_interrupt=True)]
    report = evaluate_interrupts(cases, llm=AlwaysAnswer())
    assert [case.ticket_id for case in report.missed] == ["a"]
    assert report.miss_rate == 1.0


def test_interrupts_detects_a_stop_that_was_not_needed() -> None:
    report = evaluate_interrupts([fixture(id="a")], llm=AlwaysEscalate())
    assert [case.ticket_id for case in report.spurious] == ["a"]
    assert report.fatigue_rate == 1.0


def test_a_clean_sweep_reports_no_failures() -> None:
    report = evaluate_interrupts([fixture(id="a")], llm=AlwaysAnswer())
    assert report.missed == ()
    assert report.spurious == ()
    assert report.correct == 1


def test_the_gate_kind_is_recorded() -> None:
    cases = [fixture(id="a", expected_route="escalate", must_interrupt=True)]
    report = evaluate_interrupts(cases, llm=AlwaysEscalate())
    assert report.cases[0].gate_kind == "handoff"


def test_the_shipped_fixtures_never_miss_a_required_stop() -> None:
    report = evaluate_interrupts(load_fixtures(), llm=OfflineSupportLLM())
    assert report.missed == (), [case.ticket_id for case in report.missed]


def test_a_ticket_that_raises_does_not_stop_the_sweep() -> None:
    class Exploding:
        def __call__(self, request: LLMRequest) -> str:
            if request.tag == "compose":
                raise RuntimeError("composer down")
            return json.dumps({"route": "answer", "confidence": 0.9})

    report = evaluate_interrupts([fixture(id="a"), fixture(id="b")], llm=Exploding())
    assert len(report.errors) == 2
    assert report.cases == ()


def test_the_interrupt_report_renders() -> None:
    rendered = evaluate_interrupts(load_fixtures(), llm=OfflineSupportLLM()).render()
    assert "must stop" in rendered
    assert "fatigue rate" in rendered


# -- resume -------------------------------------------------------------


def test_scenario_selection_is_deterministic() -> None:
    cases = load_fixtures()
    assert select_scenarios(cases, limit=4) == select_scenarios(cases, limit=4)


def test_scenario_selection_covers_the_gate_first() -> None:
    picked = select_scenarios(load_fixtures(), limit=4)
    tools = ToolRegistry()
    assert any(row.expected_tool and tools.is_irreversible(row.expected_tool) for row in picked)


def test_scenario_selection_spreads_across_shapes() -> None:
    picked = select_scenarios(load_fixtures(), limit=4)
    assert len({row.expected_route for row in picked}) >= 2


def test_the_resume_sweep_is_clean_on_the_shipped_fixtures() -> None:
    report = evaluate_resume(load_fixtures(), llm=OfflineSupportLLM(), limit=2)
    assert report.mismatches == ()
    assert report.duplicates == ()
    assert report.errors == ()
    assert report.clean is True


def test_the_resume_sweep_reaches_real_boundaries() -> None:
    report = evaluate_resume(load_fixtures(), llm=OfflineSupportLLM(), limit=2)
    assert len(report.reached) > 0
    assert len(report.deterministic) > 0


def test_the_effect_windows_are_reported_as_in_doubt_not_as_passes() -> None:
    report = evaluate_resume(load_fixtures(), llm=OfflineSupportLLM(), limit=2)
    assert {item.crash_point for item in report.in_doubt} == {
        "act:claimed",
        "act:executed",
        "act:recorded",
    }


def test_an_in_doubt_run_reports_the_executions_the_ledger_can_prove() -> None:
    """A crash after the execution row lands is work that happened, and it counts."""
    report = evaluate_resume(load_fixtures(), llm=OfflineSupportLLM(), limit=2)
    by_point = {item.crash_point: item.executions for item in report.in_doubt}
    assert by_point["act:claimed"] == 0
    assert by_point["act:executed"] == 0
    assert by_point["act:recorded"] == 1


def test_unreachable_boundaries_are_not_counted_as_passes() -> None:
    report = evaluate_resume(
        [fixture(id="only-answer")], llm=OfflineSupportLLM(), limit=1
    )
    statuses = {item.status for item in report.boundaries}
    assert "not_reached" in statuses
    assert not any(item.status == "deterministic" for item in report.boundaries if item.crash_point == "act:enter")


def degraded_resume(status: str, boundaries: int = 13) -> ResumeReport:
    """A sweep whose every boundary came back with the same status."""
    return ResumeReport(
        scenarios=(
            ScenarioResult(
                ticket_id="TCK-023",
                baseline_trail=("classify", "plan", "human_gate", "act", "compose"),
                baseline_executions=1,
                boundaries=tuple(
                    BoundaryResult(
                        ticket_id="TCK-023",
                        crash_point=f"point-{index}",
                        status=status,
                    )
                    for index in range(boundaries)
                ),
            ),
        )
    )


def gate_on(resume: ResumeReport) -> EvalReport:
    """The shipped gate, with the resume section swapped for a rigged one."""
    return replace(run_evaluation(skip_resume=True), resume=resume)


def test_a_sweep_that_reached_nothing_fails_the_gate() -> None:
    """The failure mode a "no mismatches" check cannot see.

    Every boundary reads not_reached, so there is nothing to mismatch and
    nothing to duplicate. It is a harness that stopped testing, and it used to
    pass.
    """
    report = gate_on(degraded_resume("not_reached"))
    assert report.passed is False
    assert any("not exercising the graph" in reason for reason in report.failures)


def test_a_sweep_that_ended_entirely_in_doubt_fails_the_gate() -> None:
    """Every resume gave up. Nothing was proved deterministic, and that is a fail."""
    report = gate_on(degraded_resume("in_doubt"))
    assert report.passed is False
    assert any("resume determinism" in reason for reason in report.failures)
    assert any("in doubt rate" in reason for reason in report.failures)


def test_a_healthy_sweep_passes_the_gate() -> None:
    report = gate_on(degraded_resume("deterministic"))
    assert report.passed is True, report.failures


def test_the_shipped_sweep_clears_the_resume_thresholds() -> None:
    """The thresholds are calibrated against the smallest sample, not the default one."""
    resume = evaluate_resume(load_fixtures(), llm=OfflineSupportLLM(), limit=1)
    limits = load_thresholds()
    assert resume.reached_rate >= limits.min_resume_reached_rate
    assert resume.deterministic_rate >= limits.min_resume_deterministic_rate
    assert resume.in_doubt_rate <= limits.max_resume_in_doubt_rate


def test_the_resume_report_renders_and_serialises() -> None:
    report = evaluate_resume(load_fixtures(), llm=OfflineSupportLLM(), limit=1)
    assert "boundaries" in report.render()
    assert report.to_dict()["scenarios"] == 1


# -- thresholds and the gate --------------------------------------------


def test_the_shipped_thresholds_load() -> None:
    limits = load_thresholds()
    assert 0.0 < limits.min_routing_accuracy <= 1.0
    assert limits.max_interrupt_miss_rate == 0.0


def test_thresholds_can_be_read_from_a_file(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"min_routing_accuracy": 0.99}), encoding="utf-8")
    assert load_thresholds(path).min_routing_accuracy == 0.99


def test_an_unknown_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown threshold"):
        Thresholds.from_dict({"min_accuracy_typo": 0.9})


def test_a_missing_threshold_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_thresholds(tmp_path / "absent.json")


def test_the_gate_passes_on_the_shipped_fixtures() -> None:
    report = run_evaluation(skip_resume=True)
    assert report.passed, report.failures


def test_the_gate_fails_when_accuracy_drops() -> None:
    report = run_evaluation(
        llm=AlwaysAnswer(),
        thresholds=Thresholds(min_routing_accuracy=0.99),
        skip_resume=True,
    )
    assert report.passed is False
    assert any("routing accuracy" in reason for reason in report.failures)


def test_the_gate_fails_on_a_single_missed_interrupt() -> None:
    report = run_evaluation(
        fixtures=[fixture(id="a", expected_route="escalate", must_interrupt=True)],
        llm=AlwaysAnswer(),
        thresholds=Thresholds(
            min_routing_accuracy=0.0,
            min_macro_f1=0.0,
            min_tool_accuracy=0.0,
        ),
        skip_resume=True,
    )
    assert report.passed is False
    assert any("missed interrupt" in reason for reason in report.failures)


def test_the_gate_fails_on_approval_fatigue() -> None:
    report = run_evaluation(
        fixtures=[fixture(id=f"f-{index}") for index in range(4)],
        llm=AlwaysEscalate(),
        thresholds=Thresholds(
            min_routing_accuracy=0.0,
            min_macro_f1=0.0,
            min_tool_accuracy=0.0,
            max_interrupt_fatigue_rate=0.1,
        ),
        skip_resume=True,
    )
    assert any("fatigue" in reason for reason in report.failures)


def test_the_gate_fails_on_an_inconsistent_fixture_file() -> None:
    report = run_evaluation(
        fixtures=[fixture(id="a", expected_route="escalate", must_interrupt=False)],
        llm=AlwaysEscalate(),
        thresholds=Thresholds(
            min_routing_accuracy=0.0, min_macro_f1=0.0, min_tool_accuracy=0.0
        ),
        skip_resume=True,
    )
    assert any("inconsistent" in reason for reason in report.failures)


def test_the_full_report_renders_the_gate_verdict() -> None:
    rendered = run_evaluation(skip_resume=True).render()
    assert "GATE: pass" in rendered
    assert "ROUTING" in rendered
    assert "INTERRUPTS" in rendered


def test_a_failing_report_prints_every_reason() -> None:
    report = run_evaluation(
        llm=AlwaysAnswer(),
        thresholds=Thresholds(min_routing_accuracy=0.99, min_macro_f1=0.99),
        skip_resume=True,
    )
    rendered = report.render()
    assert "GATE: fail" in rendered
    assert rendered.count("  routing") >= 1


def test_the_report_serialises_to_json() -> None:
    payload = run_evaluation(skip_resume=True).to_dict()
    assert payload["passed"] is True
    assert payload["resume"] is None
    assert json.dumps(payload)


def test_the_report_is_a_frozen_value(offline_llm) -> None:
    report = run_evaluation(skip_resume=True)
    assert isinstance(report, EvalReport)
    with pytest.raises(Exception):
        report.thresholds.min_routing_accuracy = 0.1  # type: ignore[misc]
