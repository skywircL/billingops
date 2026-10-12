"""A provider that cannot serve calls is the environment failing, not the agent.

Found in runs/cov1-coverage-v1: the DeepSeek balance ran out (HTTP 402) and 51 trials
were recorded as agent results — B's as unclassified failures, A's as passes and fails
reached on regex extraction and fallback text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from billingops.evaluation import trials as trials_mod
from billingops.evaluation.suite import PROVIDER_STOP_AFTER, run_suite
from billingops.evaluation.tasks import load_tasks
from billingops.evaluation.trials import classify_failure, run_trial
from billingops.llm.base import Message, ModelConfig, ModelError
from billingops.llm.openai_compat import OpenAICompatibleModel

from test_openai_compat import StubClient

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "data" / "tasks" / "gold-v1.json"


class _SDKError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


def _named(name: str) -> type[_SDKError]:
    return type(name, (_SDKError,), {})


class _Provider:
    """Every call fails the way the adapter reports an SDK error."""

    def __init__(self, status: int | None, provider_error: str = "APIStatusError") -> None:
        self.status, self.provider_error = status, provider_error

    def generate(self, messages, tool_schemas, config):
        raise ModelError(f"{self.provider_error}: provider refused", provider_error=self.provider_error, status=self.status)


@pytest.mark.parametrize("cls,status,infra", [
    ("APIStatusError", 402, True),        # Insufficient Balance
    ("RateLimitError", 429, True),
    ("InternalServerError", 503, True),
    ("AuthenticationError", 401, True),
    ("APIConnectionError", None, True),
    ("APITimeoutError", None, True),
    ("BadRequestError", 400, False),      # the request we built was rejected: our own
    ("RuntimeError", None, False),
])
def test_the_adapter_keeps_the_provider_class_and_status(cls, status, infra) -> None:
    client = StubClient(_named(cls)("provider says no", status))
    with pytest.raises(ModelError) as caught:
        OpenAICompatibleModel(client=client).generate([Message(role="user", content="u")], [], ModelConfig(tag="extract", model="m"))
    assert (caught.value.provider_error, caught.value.status, caught.value.infra) == (cls, status, infra)


def test_classification_reads_the_recorded_fields_not_the_message() -> None:
    infra = [{"tag": "extract", "error": "ModelError", "provider_error": "APIStatusError", "status": 402, "infra": True}]
    own = [{"tag": "investigate", "error": "ModelError", "provider_error": "BadRequestError", "status": 400, "infra": False}]
    # A trial that finished on its fallbacks is still not a measurement of the agent.
    assert classify_failure(error=None, exc=None, task_errors=[], state="closed", kinds=[], model_failures=infra) == "infra_model"
    assert classify_failure(error=None, exc=None, task_errors=[], state="error", kinds=[], model_failures=own) == "own"
    # Text that merely names an SDK class is not evidence.
    assert classify_failure(error=None, exc=None, task_errors=["ModelError: RateLimitError: 429"], state="error", kinds=[]) == "own"
    # A non-infra failure the agent recovered from leaves the trial to the scorers.
    assert classify_failure(error=None, exc=None, task_errors=[], state="closed", kinds=[], model_failures=own) is None


def _task(task_id: str):
    return next(t for t in load_tasks(GOLD) if t.task_id == task_id)


def test_a_case_finished_on_fallbacks_during_an_outage_is_unknown(billing, tmp_path) -> None:
    r = run_trial(_task("gold-v1/D10-named-invoice/zh-1"), "A", model_spec="fake", billing=billing, workdir=tmp_path / "dbs", model=_Provider(402))
    assert r.final_state == "closed"                     # regex extraction, rules and fallback text carried it
    assert (r.failure_class, r.status) == ("infra_model", "unknown")
    assert r.model_failures and all(f["status"] == 402 and f["infra"] for f in r.model_failures)


@pytest.mark.parametrize("status,failure_class,outcome", [(402, "infra_model", "unknown"), (400, "own", "fail")])
def test_an_investigation_stopped_by_the_provider_is_classified_by_status(billing, tmp_path, status, failure_class, outcome) -> None:
    r = run_trial(_task("gold-v1/D10-named-invoice/zh-1"), "B", model_spec="fake", billing=billing, workdir=tmp_path / "dbs", model=_Provider(status))
    assert r.final_state == "error"
    assert (r.failure_class, r.status) == (failure_class, outcome)


def test_the_suite_stops_once_the_provider_keeps_refusing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(trials_mod, "build_trial_model", lambda task, spec: _Provider(402))
    ids = ["gold-v1/D04-fully-refunded/zh-1", "gold-v1/D13-same-period-invoices/zh-1", "gold-v1/S01-completed/zh-1", "gold-v1/S05-no-record/zh-1"]
    status = run_suite(tasks_path=GOLD, out_dir=tmp_path / "run", groups=("A", "B"), trials=1, key_risk_trials=1, model_spec="fake", workers=1, task_ids=ids)
    assert status["stopped_reason"] == "provider_unavailable" and not status["complete"]
    assert status["recorded"] == PROVIDER_STOP_AFTER < status["planned"] == 8


def test_an_older_run_is_reclassified_from_event_kinds_and_times_only(tmp_path) -> None:
    import json

    from billingops.evaluation.trials import reclassify_provider_outage

    run = tmp_path / "run"
    (run / "artifacts").mkdir(parents=True)
    (run / "manifest.json").write_text("{}", encoding="utf-8")
    arts = {
        "during": [{"kind": "model_error", "at": "2026-09-27T08:10:37+00:00", "payload": {"tag": "extract", "error": "anything"}}],
        "before": [{"kind": "model_error", "at": "2026-09-27T07:00:00+00:00", "payload": {"tag": "investigate", "error": "Error code: 402"}}],
        "clean": [{"kind": "extracted", "at": "2026-09-27T08:10:40+00:00", "payload": {}}],
    }
    rows = []
    for name, events in arts.items():
        (run / "artifacts" / f"{name}.json").write_text(json.dumps({"events": events}), encoding="utf-8")
        rows.append({"trial_id": name, "status": "pass", "failure_class": None, "artifact": {"path": f"artifacts/{name}.json"}})
    (run / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    out = reclassify_provider_outage(run, since="2026-09-27T08:10:36+00:00", evidence="probe: 402")
    assert out["reclassified"] == 1
    got = {r["trial_id"]: r for r in map(json.loads, (run / "reclass-infra-1" / "results.jsonl").read_text(encoding="utf-8").splitlines())}
    assert (got["during"]["status"], got["during"]["failure_class"]) == ("unknown", "infra_model")
    assert got["during"]["reclassified"]["from"] == {"status": "pass", "failure_class": None}
    assert got["before"]["status"] == got["clean"]["status"] == "pass"  # message text naming 402 is not evidence
    with pytest.raises(FileExistsError):
        reclassify_provider_outage(run, since="2026-09-27T08:10:36+00:00", evidence="again")
