"""Bounded dispatch and provider circuit tests; no HTTP/model calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import threading

import pytest

from billingops.evaluation import v2_runner as runner


@pytest.fixture
def dispatch_environment(tmp_path, monkeypatch):
    """Keep actual run_bundle scheduling/artifact writes, stub only trial work."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(runner, "ROOT", workspace)
    monkeypatch.setattr(runner, "source_manifest", lambda: {
        "provenance": "before_execution", "source_digests": {},
    })
    tasks = [{"candidate_id": f"G{i:02}", "task_id": f"test/G{i:02}",
              "root_seed_family": "test-only"} for i in range(1, 9)]
    bundle = {"tasks": tasks}
    plan = {"runner_version": runner.RUNNER_VERSION, "mode": "pilot", "seed": 12,
            "candidate_ids": [t["candidate_id"] for t in tasks],
            "planned_trials": {t["candidate_id"]: 1 for t in tasks},
            "total_trials": len(tasks), "purpose": "offline_dispatch_test"}
    monkeypatch.setattr(runner, "make_plan", lambda *args, **kwargs: deepcopy(plan))
    return bundle, tmp_path / "new-run", plan


def trial_result(task, index, *, role=None, http_status=None):
    metrics = {"calls": 1, "seconds": .01, "cost_usd_estimated": 0, "errors": []}
    result = {"candidate_id": task["candidate_id"], "trial_index": index,
              "status": "unknown" if role else "pass", "latency_s": .1,
              **{f"{r}_metrics": deepcopy(metrics) for r in ("agent", "customer", "judge")}}
    if role:
        result[f"{role}_metrics"]["errors"] = [{"index": 0, "tag": "offline",
            "error": "ModelError", "provider_error": "APIStatusError", "status": http_status}]
    return result


class RecordingExecutor(ThreadPoolExecutor):
    """Track submitted but unfinished Futures, including queued work."""
    instances = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tracking_lock = threading.Lock()
        self.outstanding = self.maximum_outstanding = self.submitted_count = 0
        self.instances.append(self)

    def submit(self, fn, /, *args, **kwargs):
        future = super().submit(fn, *args, **kwargs)
        with self.tracking_lock:
            self.submitted_count += 1
            self.outstanding += 1
            self.maximum_outstanding = max(self.maximum_outstanding, self.outstanding)
        def finished(_):
            with self.tracking_lock:
                self.outstanding -= 1
        future.add_done_callback(finished)
        return future


@pytest.mark.parametrize("role,http_status", [("agent", 402), ("customer", 402), ("judge", 401), ("judge", 402)])
def test_terminal_provider_error_drains_only_in_flight_and_preserves_missing(
        dispatch_environment, monkeypatch, role, http_status):
    bundle, out, original_plan = dispatch_environment
    workers = 3
    first_cohort = threading.Barrier(workers, timeout=5)
    drain = threading.Event()
    lock = threading.Lock()
    started, emitted = [], {}
    frozen_manifest_bytes = []
    original_write = runner.write_json

    def record_stop(path, value):
        original_write(path, value)
        if path.name == "provider-stop.json":
            assert value["status"] == "stopped_provider_terminal_error"
            assert len(started) == workers  # A whole bounded cohort was started.
            frozen_manifest_bytes.append((out / "manifest.json").read_bytes())
            drain.set()  # Only already-running trials may now finish.

    def fake_trial(task, index, directory):
        with lock:
            position = len(started)
            started.append(task["candidate_id"])
        if position < workers:
            first_cohort.wait()
        if position != 0:
            assert drain.wait(5), "runner did not record a stop before draining"
        result = trial_result(task, index, role=role if position == 0 else None,
                              http_status=http_status)
        emitted[task["candidate_id"]] = deepcopy(result)
        return result

    monkeypatch.setattr(runner, "write_json", record_stop)
    monkeypatch.setattr(runner, "run_trial", fake_trial)
    monkeypatch.setattr(runner, "ThreadPoolExecutor", RecordingExecutor)
    report = runner.run_bundle(bundle, out, mode="pilot", selected=None, workers=workers)
    pool = RecordingExecutor.instances[-1]
    assert pool.maximum_outstanding <= workers
    assert pool.submitted_count == len(started) == report["recorded_trials"] == workers
    assert report["status"] == "stopped_provider_terminal_error"
    assert report["planned_trials"] == original_plan["total_trials"] == 8
    assert report["missing_trials"] == 5 and not report["registered_plan_complete"]
    assert report["in_flight_remaining"] == 0 and not report["summary_written"]
    assert not (out / "summary.json").exists()
    assert "task_equal_S_lower" not in report and "task_equal_S_upper" not in report
    assert report["provider_terminal_errors"][0]["role"] == role
    assert report["provider_terminal_errors"][0]["status"] == http_status
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert {r["candidate_id"] for r in rows} == set(started)
    assert all(r == emitted[r["candidate_id"]] for r in rows)  # No rewrite as pass/fail.
    assert sum(r["status"] == "pass" for r in rows) == 2
    expected = {f"{cid}-t0" for cid in original_plan["candidate_ids"]}
    completed, missing = set(report["completed_trial_ids"]), set(report["registered_missing_trial_ids"])
    assert completed.isdisjoint(missing) and completed | missing == expected
    assert {x["trial_id"] for x in report["registered_missing_trials"]} == missing
    assert (out / "manifest.json").read_bytes() == frozen_manifest_bytes[0]
    assert json.loads((out / "fixtures.json").read_text()) == bundle
    assert json.loads((out / "incomplete-run.json").read_text()) == report


@pytest.mark.parametrize("http_status", [None, 429, 500])
def test_nonterminal_errors_keep_complete_registered_normal_behavior(
        dispatch_environment, monkeypatch, http_status):
    bundle, out, plan = dispatch_environment
    def fake_trial(task, index, directory):
        return trial_result(task, index, role="judge" if http_status else None,
                            http_status=http_status)
    monkeypatch.setattr(runner, "run_trial", fake_trial)
    monkeypatch.setattr(runner, "ThreadPoolExecutor", RecordingExecutor)
    report = runner.run_bundle(bundle, out, mode="pilot", selected=None, workers=2)
    pool = RecordingExecutor.instances[-1]
    assert pool.maximum_outstanding <= 2 and pool.submitted_count == 8
    assert report["planned_trials"] == report["recorded_trials"] == 8
    assert all(case["missing"] == 0 for case in report["by_task"])
    assert report["task_equal_S_lower"] == (1 if http_status is None else 0)
    assert not (out / "provider-stop.json").exists()
    assert not (out / "incomplete-run.json").exists()
    assert json.loads((out / "summary.json").read_text()) == report
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["planned_trials"] == plan["planned_trials"]
    assert manifest["provider_terminal_error_policy"]["http_statuses"] == [401, 402]
    assert manifest["provider_terminal_error_policy"]["max_in_flight_trials"] == 2
    assert manifest["runner_version"] == runner.RUNNER_VERSION


def test_still_running_terminal_errors_are_all_preserved(dispatch_environment, monkeypatch):
    bundle, out, _ = dispatch_environment
    barrier = threading.Barrier(2, timeout=5)
    def fake_trial(task, index, directory):
        barrier.wait()
        return trial_result(task, index, role="judge", http_status=402)
    monkeypatch.setattr(runner, "run_trial", fake_trial)
    report = runner.run_bundle(bundle, out, mode="pilot", selected=None, workers=2)
    assert report["recorded_trials"] == 2 and report["missing_trials"] == 6
    assert len(report["provider_terminal_errors"]) == 2
    assert len({e["trial_id"] for e in report["provider_terminal_errors"]}) == 2


def test_existing_archive_is_never_reopened_or_modified(dispatch_environment, monkeypatch):
    bundle, out, _ = dispatch_environment
    out.mkdir()
    (out / "manifest.json").write_text('frozen original manifest\n')
    (out / "results.jsonl").write_text('original result bytes\n')
    original = {p.name: p.read_bytes() for p in out.iterdir()}
    monkeypatch.setattr(runner, "run_trial", lambda *args: pytest.fail("existing run must not execute"))
    with pytest.raises(FileExistsError):
        runner.run_bundle(bundle, out, mode="pilot", selected=None, workers=2)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == original


@pytest.mark.parametrize("stopped,expected_exit", [(True, 2), (False, None)])
def test_cli_provider_stop_returns_nonzero_without_model_call(tmp_path, monkeypatch, capsys,
                                                              stopped, expected_exit):
    source = tmp_path / "input.json"
    source.write_text('{"tasks": []}')
    report = {"status": "stopped_provider_terminal_error", "missing_trials": 5} if stopped else {"recorded_trials": 8}
    monkeypatch.setattr(runner, "run_bundle", lambda *args, **kwargs: report)
    monkeypatch.setattr("sys.argv", ["v2_runner", str(source), "--out", str(tmp_path / "out")])
    if expected_exit:
        with pytest.raises(SystemExit) as exc:
            runner.main()
        assert exc.value.code == expected_exit
    else:
        runner.main()
    assert json.loads(capsys.readouterr().out) == report
