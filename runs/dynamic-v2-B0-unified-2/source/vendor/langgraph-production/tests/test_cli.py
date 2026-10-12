from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from langgraph_production.__main__ import main as agent_main
from langgraph_production.evaluation.__main__ import main as eval_main


def test_the_eval_cli_passes_on_the_shipped_fixtures(capsys: pytest.CaptureFixture[str]) -> None:
    assert eval_main(["--skip-resume"]) == 0
    assert "GATE: pass" in capsys.readouterr().out


def test_the_eval_cli_writes_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "nested" / "report.json"
    assert eval_main(["--skip-resume", "--json", str(target)]) == 0
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert "routing" in payload
    capsys.readouterr()


def test_the_eval_cli_returns_one_when_a_threshold_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    thresholds = tmp_path / "strict.json"
    thresholds.write_text(json.dumps({"min_routing_accuracy": 1.0}), encoding="utf-8")
    assert eval_main(["--skip-resume", "--thresholds", str(thresholds)]) == 1
    assert "GATE: fail" in capsys.readouterr().out


def test_the_eval_cli_runs_the_crash_sweep(capsys: pytest.CaptureFixture[str]) -> None:
    assert eval_main(["--resume-limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "RESUME" in out
    assert "deterministic" in out


def test_the_agent_cli_runs_a_ticket(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_main(["--text", "How do I export a dashboard to CSV?"]) == 0
    out = capsys.readouterr().out
    assert "OUTCOME    answered" in out


def test_the_agent_cli_shows_the_gate(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_main(["--fixture", "TCK-023"]) == 0
    out = capsys.readouterr().out
    assert "GATE (approval)" in out
    assert "OUTCOME    acted" in out
    assert "handler executions recorded on disk: 1" in out


def test_the_agent_cli_can_deny(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_main(["--fixture", "TCK-023", "--deny"]) == 0
    out = capsys.readouterr().out
    assert "OUTCOME    denied" in out
    assert "handler executions recorded on disk: 0" in out


def test_the_agent_cli_survives_an_injected_crash(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_main(["--fixture", "TCK-023", "--crash-at", "act:after_effect"]) == 0
    out = capsys.readouterr().out
    assert "CRASHES    1" in out
    assert "replayed from the store: True" in out
    assert "handler executions recorded on disk: 1" in out


def test_the_agent_cli_does_not_claim_a_resume_that_never_happened(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Zero crashes means nothing was resumed, and the line used to say otherwise."""
    assert agent_main(["--fixture", "TCK-023"]) == 0
    out = capsys.readouterr().out
    assert "CRASHES    0" in out
    assert "resumed from the checkpoint" not in out


def test_the_agent_cli_keeps_the_store_when_asked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "state.sqlite3"
    assert agent_main(["--fixture", "TCK-023", "--db", str(db_path)]) == 0
    capsys.readouterr()
    assert db_path.exists(), "the run has to leave its checkpoints where it was told to"


def test_the_agent_cli_cleans_up_the_store_it_invented(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A demo that litters temporary databases is a demo nobody runs twice."""
    pattern = "langgraph-production-demo-*"
    before = set(Path(tempfile.gettempdir()).glob(pattern))
    assert agent_main(["--fixture", "TCK-023"]) == 0
    capsys.readouterr()
    assert set(Path(tempfile.gettempdir()).glob(pattern)) == before


def test_the_agent_cli_can_print_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_main(["--fixture", "TCK-001", "--json"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{") :])
    assert payload["outcome"] == "answered"


def test_the_agent_cli_rejects_an_unknown_fixture() -> None:
    with pytest.raises(SystemExit):
        agent_main(["--fixture", "NOPE-1"])


def test_the_agent_cli_requires_a_ticket() -> None:
    with pytest.raises(SystemExit):
        agent_main([])
