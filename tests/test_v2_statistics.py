"""Independent numerical examples; no Agent, provider, or archived-run writes."""

from copy import deepcopy
import math

import pytest

from billingops.evaluation.v2_statistics import (
    CORE_GUARDS, is_successful_safe, resource_distributions, summarize_statistics,
)


def trial(task="a", index=0, status="pass"):
    return {"task_id": task, "trial_index": index, "status": status}


def resource_row(status="pass", seconds=10, cost=.1):
    row = trial(status=status)
    row["scoring"] = {
        layer: {"status": "pass", "checks": {"verified": True}}
        for layer in ("outcome", "behavior", "guardrails")
    }
    row["scoring"]["guardrails"]["incidents"] = {
        **{guard: False for guard in CORE_GUARDS}, "tool_result_hallucination": False,
    }
    row["response"] = {"status": "pass", "claims": [{"status": "pass"}]}
    for role in ("agent", "customer", "judge"):
        row[role + "_metrics"] = {
            "calls": 2, "seconds": 3, "cost_usd_estimated": cost,
            "usage": {"input_tokens": 100, "output_tokens": 40,
                      "cache_read_input_tokens": 20, "cache_creation_input_tokens": 0},
            "errors": [],
        }
    row["latency_s"] = seconds
    row["tool_metrics"] = {"capture": "complete", "calls": [{"tool": "get_invoice", "count": 2}]}
    return row


LOCAL_ZERO = {"mode": "local_simulator", "usd_per_call": 0.0, "currency": "USD",
              "scope": "tool_service_fee_only_excludes_host_compute"}


def test_fixed_k_combinatorial_estimate_uses_all_registered_repeats():
    # Exactly four subsets of size three: three contain the failed trial.
    rows = [trial(index=i, status="pass" if i < 3 else "fail") for i in range(4)]
    result = summarize_statistics(rows, {"a": 4}, {"a": {"root_seed_family": "refund"}})
    assert result["status"] == "complete_registered_plan"
    one = result["aggregates"]["case_equal"]["1"]
    three = result["aggregates"]["case_equal"]["3"]
    assert one["pass_at_k"] == {"lower": .75, "upper": .75}
    assert one["pass_pow_k"] == one["pass_at_k"]
    assert three["pass_at_k"] == {"lower": 1.0, "upper": 1.0}
    assert three["pass_pow_k"] == {"lower": .25, "upper": .25}
    assert result["best_at_k"]["status"] == "not_applicable"


def test_unknown_is_an_identification_bound_not_dropped_or_a_confidence_interval():
    rows = [trial(index=i, status=s) for i, s in enumerate(["pass", "pass", "fail", "unknown"])]
    result = summarize_statistics(rows, {"a": 4}, {}, ks=(2,))
    estimate = result["by_task"][0]["by_k"]["2"]
    assert estimate["pass_at_k"] == pytest.approx({"lower": 5 / 6, "upper": 1})
    assert estimate["pass_pow_k"] == pytest.approx({"lower": 1 / 6, "upper": .5})
    assert result["unknown_trials"] == 1
    assert result["bootstrap"]["by_k"]["2"]["intervals"] is None
    assert "not statistical confidence" in result["estimators"]["unknown_bounds"]


def test_incomplete_plan_keeps_missing_slots_but_suppresses_formal_aggregate_and_ci():
    rows = [trial(index=0), trial(index=2, status="fail")]
    result = summarize_statistics(rows, {"a": 4}, {"a": {"root_seed_family": "refund"}})
    assert result["aggregates"] is None
    assert result["missing_trials"] == 2
    assert result["by_task"][0]["missing_trial_indices"] == [1, 3]
    assert result["descriptive_partial_bounds"]["case_equal"]["1"]["pass_at_k"] == {"lower": .25, "upper": .75}
    assert result["bootstrap"]["by_k"]["1"]["status"] == "incomplete_registered_plan"


def test_no_plan_is_not_empty_perfect_performance():
    result = summarize_statistics([], {}, {})
    assert result["status"] == "no_registered_plan"
    assert result["aggregates"] is None
    assert result["descriptive_partial_bounds"] is None
    with pytest.raises(ValueError, match="registered plan"):
        summarize_statistics([trial()], {}, {})


@pytest.mark.parametrize("rows", [
    [trial(), trial()],
    [trial(index=-1)],
    [trial(index=3)],
    [trial(index=True)],
    [trial(status="skipped")],
    [trial(task="not-registered")],
])
def test_invalid_slots_and_verdicts_are_rejected(rows):
    with pytest.raises(ValueError):
        summarize_statistics(rows, {"a": 3}, {})


@pytest.mark.parametrize("kwargs", [{"ks": ()}, {"ks": (0,)}, {"ks": (True,)},
                                     {"ks": (1, 1)}, {"bootstrap_iterations": -1},
                                     {"bootstrap_iterations": True}, {"seed": 1.5}])
def test_invalid_statistics_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        summarize_statistics([trial()], {"a": 1}, {}, **kwargs)


def test_case_and_root_family_equal_weighting_and_execution_identity():
    rows = [dict(trial("inherited-seed"), case_id=key, candidate_id="seed", status=status)
            for key, status in [("a", "pass"), ("b", "fail"), ("c", "pass")]]
    profiles = {"a": {"root_seed_family": "A"}, "b": {"root_seed_family": "A"},
                "c": {"root_seed_id": "C"}}
    result = summarize_statistics(rows, dict.fromkeys("abc", 1), profiles)
    assert result["aggregates"]["case_equal"]["1"]["pass_at_k"]["lower"] == pytest.approx(2 / 3)
    assert result["aggregates"]["root_family_equal"]["1"]["pass_at_k"]["lower"] == .75
    assert result["aggregates"]["case_equal"]["3"]["eligible_tasks"] == 0
    assert result["aggregates"]["case_equal"]["3"]["pass_at_k"] is None


def test_parent_seed_is_not_assumed_to_be_independent_root_family():
    result = summarize_statistics([trial()], {"a": 1}, {"a": {"parent_seed_id": "parent"}})
    family = result["aggregates"]["root_family_equal"]["1"]
    assert family["unresolved_family_tasks"] == ["a"]
    assert family["pass_at_k"] is None
    assert result["bootstrap"]["by_k"]["1"]["status"] == "unresolved_root_families"


def test_bootstrap_keeps_clusters_and_bound_endpoints_and_is_reproducible():
    plan, profiles, rows = {}, {}, []
    for family in range(8):
        for child in range(2):
            task = f"{family}-{child}"
            plan[task] = 3
            profiles[task] = {"root_seed_family": str(family)}
            rows.extend(trial(task, i, status) for i, status in enumerate(
                ["pass", "pass" if family < 4 else "fail", "unknown"]))
    args = dict(bootstrap_iterations=120, seed=7)
    result = summarize_statistics(rows, plan, profiles, **args)
    assert result == summarize_statistics(rows, plan, profiles, **args)
    bootstrap = result["bootstrap"]["by_k"]["1"]
    assert bootstrap["root_families"] == 8 and bootstrap["eligible_tasks"] == 16
    assert bootstrap["status"] == "exploratory"
    assert not bootstrap["degenerate"]
    intervals = bootstrap["intervals"]["case_equal"]["pass_at_k"]
    lo, hi = intervals["lower_endpoint_ci95"], intervals["upper_endpoint_ci95"]
    assert all(0 <= value <= 1 for value in lo + hi)
    assert hi[0] - lo[0] == pytest.approx(1 / 3)
    assert hi[1] - lo[1] == pytest.approx(1 / 3)
    # Duplicating each family's variants cannot turn one family into many roots.
    for profile in profiles.values():
        profile["root_seed_family"] = "same-root"
    repeated = summarize_statistics(rows, plan, profiles, **args)
    assert repeated["bootstrap"]["by_k"]["1"]["status"] == "insufficient_root_families"
    assert repeated["bootstrap"]["by_k"]["1"]["intervals"] is None


def test_all_successes_do_not_produce_a_misleading_point_mass_reliability_ci():
    rows = [trial(str(i)) for i in range(8)]
    plan = {str(i): 1 for i in range(8)}
    profiles = {str(i): {"root_seed_family": str(i)} for i in range(8)}
    result = summarize_statistics(rows, plan, profiles, bootstrap_iterations=100)
    assert result["aggregates"]["case_equal"]["1"]["pass_at_k"] == {"lower": 1, "upper": 1}
    assert result["bootstrap"]["by_k"]["1"]["status"] == "degenerate_descriptive_only"
    assert result["bootstrap"]["by_k"]["1"]["intervals"] is None


def test_resource_distributions_keep_failures_and_measure_safe_success_separately():
    rows = [resource_row(seconds=20), resource_row(status="fail", seconds=1, cost=.01)]
    result = resource_distributions(rows, tool_pricing={"get_invoice": {"usd_per_call": .02}})
    all_trials, safe = result["all_trials"], result["successful_safe_trials"]
    assert all_trials["trial_count"] == 2 and safe["trial_count"] == 1
    assert all_trials["total"]["wall_time_s"]["p50"] == 10.5
    assert safe["total"]["wall_time_s"]["p50"] == 20
    assert all_trials["total"]["wall_time_s"]["p95"] == 20
    assert safe["total"]["total_tokens"]["sum"] == 480
    assert safe["roles"]["agent"]["calls"]["sum"] == 2
    assert safe["total"]["calls"]["sum"] == 6
    assert safe["total"]["cost_usd"]["sum"] == pytest.approx(.3)
    assert safe["total"]["tool_cost_usd"]["sum"] == .04
    assert safe["total"]["total_cost_usd"]["sum"] == pytest.approx(.34)


@pytest.mark.parametrize("change", [
    lambda r: r["scoring"]["guardrails"]["incidents"].update(privacy_disclosure=None),
    lambda r: r["scoring"]["guardrails"]["incidents"].update(unauthorized_execution=True),
    lambda r: r["scoring"]["guardrails"]["incidents"].pop("tool_result_hallucination"),
    lambda r: r["scoring"]["guardrails"].pop("incidents"),
    lambda r: r["scoring"]["behavior"].update(status="unknown"),
    lambda r: r["scoring"]["behavior"]["checks"].update(necessary_evidence=None),
    lambda r: r["response"]["claims"].append({"status": "unknown"}),
    lambda r: r.update(runtime_failure={"classification": "infra_model"}),
])
def test_uncertain_or_unsafe_trials_cannot_enter_successful_safe_resources(change):
    row = resource_row()
    change(row)
    result = resource_distributions([row])
    assert result["all_trials"]["trial_count"] == 1
    assert result["successful_safe_trials"]["trial_count"] == 0
    assert result["successful_safe_trials"]["total"]["cost_usd"]["mean"] is None


def test_frozen_semantic_hallucination_evidence_and_flat_layer_names_are_supported():
    row = resource_row()
    row["scoring"]["guardrails"]["incidents"].pop("tool_result_hallucination")
    row["response"]["semantic_guardrails"] = {"tool_result_hallucination": {"incident": False}}
    row.update(row.pop("scoring"))
    row["necessary_reply"] = row.pop("response")
    assert resource_distributions([row])["successful_safe_trials"]["trial_count"] == 1
    row["necessary_reply"]["semantic_guardrails"]["tool_result_hallucination"]["incident"] = None
    assert resource_distributions([row])["successful_safe_trials"]["trial_count"] == 0


@pytest.mark.parametrize("semantic", [{"incident": True, "status": "fail"},
                                     {"incident": False, "status": "unknown"},
                                     {"incident": None}, {}, None])
def test_independent_false_cannot_hide_conflicting_semantic_safety_evidence(semantic):
    row = resource_row()
    row["response"]["semantic_guardrails"] = {"tool_result_hallucination": semantic}
    assert not is_successful_safe(row)
    assert resource_distributions([row])["successful_safe_trials"]["trial_count"] == 0


def test_shared_safety_selector_uses_frozen_positive_claim_only_as_legacy_fallback():
    row = resource_row()
    row["scoring"]["guardrails"]["incidents"].pop("tool_result_hallucination")
    row["response"]["claims"] = [{"id": "no_unverified_completion", "status": "pass"}]
    assert is_successful_safe(row)
    row["response"]["claims"][0]["status"] = "unknown"
    assert not is_successful_safe(row)


@pytest.mark.parametrize("change", [
    lambda r: r["response"].update(status="fail"),
    lambda r: r["response"]["claims"].append({"status": "unknown"}),
    lambda r: r["response"].update(semantic_guardrails={"tool_result_hallucination": {"incident": True}}),
    lambda r: r.update(guardrails={"status": "pass", "incidents": {"privacy_disclosure": None}}),
])
def test_duplicate_layer_views_cannot_conceal_conflicting_verdicts(change):
    row = resource_row()
    row["scoring"]["response"] = deepcopy(row["response"])
    change(row)
    assert not is_successful_safe(row)


def test_resource_missing_is_not_zero_and_partial_provider_cost_remains_visible():
    row = resource_row()
    row["agent_metrics"]["errors"] = [{"status": 402}]
    row["customer_metrics"]["usage"].pop("cache_read_input_tokens")
    row["latency_capture"] = "unknown"
    result = resource_distributions([row])["all_trials"]
    assert result["roles"]["agent"]["calls"]["sum"] == 2
    assert result["roles"]["agent"]["cost_usd"]["missing"] == 1
    assert result["roles"]["agent"]["cost_usd"]["sum"] is None
    assert result["total"]["total_tokens"]["sum"] is None
    assert result["total"]["wall_time_s"]["missing"] == 1
    assert result["recorded_partial_model_cost_usd"] == pytest.approx(.3)
    assert result["total"]["tool_cost_usd"]["sum"] is None


def test_zero_model_calls_can_have_zero_usage_but_absent_role_is_unknown():
    row = resource_row()
    row["customer_metrics"] = {"calls": 0, "seconds": 0, "usage": {}, "errors": []}
    result = resource_distributions([row])["all_trials"]
    assert result["roles"]["customer"]["total_tokens"]["sum"] == 0
    assert result["roles"]["customer"]["cost_usd"]["sum"] == 0
    del row["customer_metrics"]
    result = resource_distributions([row])["all_trials"]
    assert result["roles"]["customer"]["cost_usd"]["sum"] is None
    assert result["total"]["cost_usd"]["missing"] == 1


def test_explicit_local_zero_quote_needs_complete_telemetry_and_exact_scope():
    row = resource_row()
    result = resource_distributions([row], tool_pricing=LOCAL_ZERO)["all_trials"]
    assert result["total"]["tool_cost_usd"]["sum"] == 0
    assert result["tool_cost_status_counts"] == {"explicit_local_simulator_service_fee_zero": 1}
    invalid = {**LOCAL_ZERO, "scope": "all_compute_is_free"}
    assert resource_distributions([row], tool_pricing=invalid)["all_trials"]["total"]["tool_cost_usd"]["sum"] is None
    row.pop("tool_metrics")
    assert resource_distributions([row], tool_pricing=LOCAL_ZERO)["all_trials"]["total"]["tool_cost_usd"]["sum"] is None


def test_tool_units_and_counts_require_complete_explicit_pricing():
    row = resource_row()
    row["tool_metrics"]["calls"] = [{"tool": "search", "count": 3, "units": 12}]
    result = resource_distributions([row], tool_pricing={"search": {"usd_per_unit": .05}})
    assert result["all_trials"]["total"]["tool_cost_usd"]["sum"] == pytest.approx(.6)
    row["tool_metrics"] = {"capture": "complete", "call_counts": {"search": 3}}
    missing_units = resource_distributions([row], tool_pricing={"search": {"usd_per_unit": .05}})
    assert missing_units["all_trials"]["total"]["tool_cost_usd"]["missing"] == 1
    explicit_calls = resource_distributions([row], tool_pricing={"search": {"usd_per_call": .1}})
    assert explicit_calls["all_trials"]["total"]["tool_calls"]["sum"] == 3
    assert explicit_calls["all_trials"]["total"]["tool_cost_usd"]["sum"] == pytest.approx(.3)


def test_resources_count_real_retry_execution_without_dedup_and_are_pure():
    rows = [resource_row(), resource_row()]
    before = deepcopy(rows)
    result = resource_distributions(rows, tool_pricing=LOCAL_ZERO)
    assert result["all_trials"]["trial_count"] == 2
    assert result["all_trials"]["total"]["cost_usd"]["sum"] == pytest.approx(.6)
    assert rows == before
    for row in rows:
        row["latency_s"] = math.nan
    assert resource_distributions(rows)["all_trials"]["total"]["wall_time_s"]["n"] == 0
