"""Recovery selection must never become best-of sampling."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("controlled_retry", Path(__file__).parents[1] / "scripts/retry_controlled_v2.py")
retry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retry)


def row(i, code=402, status="unknown"):
    return {"candidate_id": "G01", "trial_index": i, "status": status,
            "judge_metrics": {"errors": [{"status": code}]}}


def test_select_provider_cause_even_original_fail_not_only_unknown():
    rows = [row(i, status="fail" if i == 0 else "unknown") for i in range(9)]
    assert retry.selection(rows + [row(10, code=429, status="fail")]) == sorted(f"G01-t{i}" for i in range(9))


@pytest.mark.parametrize("rows", [[row(i) for i in range(8)], [row(i) for i in range(10)], [row(0)] * 9])
def test_reject_wrong_or_duplicate_selection(rows):
    with pytest.raises(ValueError):
        retry.selection(rows)


def test_complete_is_not_all_pass_and_business_fail_must_replace():
    rows = [row(i, code=429, status="fail" if i == 0 else "unknown") for i in range(9)]
    exact, errors = retry.completion_check([f"G01-t{i}" for i in range(9)], rows)
    assert exact and not errors


def test_terminal_provider_error_or_duplicate_cannot_be_complete():
    selected = [f"G01-t{i}" for i in range(9)]
    exact, errors = retry.completion_check(selected, [row(i) for i in range(9)])
    assert exact and len(errors) == 9
    exact, _ = retry.completion_check(selected, [row(0, code=429)] * 9)
    assert not exact
