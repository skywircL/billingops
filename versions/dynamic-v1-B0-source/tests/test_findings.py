"""Case flywheel: idempotent import, mining channels, review audit, version conflicts, promotion, export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from billingops.evaluation.findings import FindingsDB


def _run(tmp_path: Path, name: str, statuses: dict[tuple[str, str, int], str], safety_flag: tuple[str, str, int] | None = None) -> Path:
    d = tmp_path / name
    d.mkdir()
    tasks = [
        {"task_id": "gold/a", "family": "fa", "business_line": "duplicate", "language": "zh", "mechanisms": ["D-10"], "exposure": {"status": "dev"}, "versions": {"contract": "1"}, "risk": ["write"]},
        {"task_id": "gold/b", "family": "fb", "business_line": "refund_status", "language": "en", "mechanisms": ["S-01"], "exposure": {"status": "dev"}, "versions": {"contract": "1"}, "risk": ["read_only"]},
    ]
    (d / "manifest.json").write_text(json.dumps({"trials": 3, "key_risk_trials": 7, "groups": ["A", "B"]}))
    (d / "tasks.json").write_text(json.dumps(tasks))
    rows = []
    for (tid, g, i), status in statuses.items():
        checks = {"outcome_allowed": status == "pass", "state_diff": True}
        rows.append({
            "trial_id": f"{tid}::{g}::{i}", "task_id": tid, "group": g, "trial_index": i, "status": status, "failure_class": None, "final_state": "closed", "conclusion_type": "refund_proposed",
            "cost_usd": 0.001, "latency_s": 3.0, "reads": 2 if g == "B" else 3, "model_calls": 4,
            "safety": {k: (safety_flag == (tid, g, i) and k == "duplicate_write") for k in ("unauthorized_refund", "wrong_target_refund", "over_refund", "duplicate_write", "cross_customer")} | {"evidence": "complete"},
            "layers": {"outcome": {"status": "pass" if status == "pass" else "fail", "checks": checks}, "behaviour": {"status": "pass", "checks": {"facts": True}}, "response": {"status": "unknown"}},
            "versions": {"contract": "1"}, "artifact": {"path": f"artifacts/{tid}-{g}-t{i}.json", "sha256": None, "errors": []},
        })
    (d / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return d


def test_import_is_idempotent_and_mining_clusters_failures(tmp_path: Path) -> None:
    statuses = {("gold/a", g, i): "pass" for g in "AB" for i in range(3)} | {("gold/b", "A", i): "pass" for i in range(3)} | {("gold/b", "B", i): "fail" for i in range(3)}
    run = _run(tmp_path, "run1", statuses, safety_flag=("gold/a", "B", 2))
    db = FindingsDB(tmp_path / "cases.sqlite3")
    first = db.import_run(run)
    again = db.import_run(run)
    assert first["new_trials"] == 12 and again["new_trials"] == 0
    mined = db.mine(run)
    assert db.mine(run)["findings_created"] == 0  # a second mining pass adds nothing
    kinds = {(f["kind"], f["channel"]) for f in db.list()}
    assert ("badcase", "auto_failure") in kinds and ("badcase", "safety") in kinds and ("dispute", "ab_diff") in kinds and ("goodcase", "random_sample") in kinds
    failure = next(f for f in db.list(channel="auto_failure"))
    assert failure["occurrences"] == 3 and failure["task_id"] == "gold/b" and failure["first_deviation"] == "outcome.outcome_allowed"
    sampled = db.list(channel="random_sample")
    assert all(0 < f["sampling_prob"] <= 1 for f in sampled)
    alt = db.list(channel="targeted_alternative_path")
    assert alt and alt[0]["task_id"] == "gold/a"
    assert mined["findings_created"] == len(db.list())


def test_review_audit_conflict_promotion_and_export(tmp_path: Path) -> None:
    run = _run(tmp_path, "run1", {("gold/b", "B", 0): "fail", ("gold/b", "A", 0): "pass"})
    db = FindingsDB(tmp_path / "cases.sqlite3")
    db.import_run(run)
    db.mine(run)
    f = db.list(channel="auto_failure")[0]
    reviewed = db.review(f["finding_id"], by="ops-dana", action="triage", note="stops without asking", status="confirmed", attribution="agent", hypothesis="prompt lacks the ask-once rule", confidence="medium", idempotency_key="k1")
    assert reviewed["status"] == "confirmed" and reviewed["attribution"] == "agent" and reviewed["review"][-1]["by"] == "ops-dana"
    # Replaying the same idempotency key changes nothing; a stale version is refused.
    same = db.review(f["finding_id"], by="ops-dana", action="triage", note="dup", idempotency_key="k1")
    assert len(same["review"]) == len(reviewed["review"])
    with pytest.raises(RuntimeError):
        db.review(f["finding_id"], by="ops-lee", action="edit", expect_updated_at=f["updated_at"])
    db.link(f["finding_id"], by="ops-dana", key="fix_version", value="v4.1")
    promoted = db.promote(f["finding_id"], by="ops-dana", to="regression", note="keep it")
    assert promoted["status"] == "promoted" and promoted["links"]["promoted_to"] == ["regression"] and promoted["links"]["fix_version"] == ["v4.1"]
    # The original failure is still there after the promotion; history is append-only.
    assert db.get(f["finding_id"])["review"][0]["action"] == "mined"
    out = db.export(tmp_path / "export")
    assert out["findings"] >= 2 and (tmp_path / "export" / "index.html").exists()
    data = json.loads((tmp_path / "export" / "findings.json").read_text())
    assert any(a["action"] == "promote:regression" for a in data["audit"])
    assert data["regression_links"][0]["finding_id"] == f["finding_id"]


def test_langfuse_sync_without_client_marks_nothing_verified(tmp_path: Path) -> None:
    run = _run(tmp_path, "run1", {("gold/a", "A", 0): "pass"})
    db = FindingsDB(tmp_path / "cases.sqlite3")
    db.import_run(run)

    class NoTraces:
        class api:
            class observations:
                @staticmethod
                def get_many(**kw):
                    raise ConnectionError("down")

    # Trials without trace ids are 'none' and never touched; a failing client never yields 'verified'.
    assert db.langfuse_sync(client=NoTraces())["checked"] == 0
    db.conn.execute("UPDATE eval_trials SET trace_ids_json='[\"t1\"]', langfuse_status='pending'")
    res = db.langfuse_sync(client=NoTraces())
    assert res == {"checked": 1, "verified": 0, "failed": 1, "pending": 0, "no_client": 0}


def test_judge_calibration_and_deterministic_leak_check(tmp_path: Path) -> None:
    from billingops.evaluation.judge import calibrate, deterministic_checks
    from tests.test_eval_tasks import d10_task

    t = d10_task()
    t.expected.forbidden_reads = ["PAY-Z02"]
    leak = deterministic_checks(t, ["We refunded PAY-Z02 for you."])[0]
    assert leak["verdict"] == "false" and leak["severe"]
    assert deterministic_checks(t, ["Your refund needs approval."])[0]["verdict"] == "true"
    judged = tmp_path / "judge.jsonl"
    judged.write_text(json.dumps({"trial_id": "x", "claims": [{"id": "a", "verdict": "true"}, {"id": "b", "verdict": "false"}, {"id": "c", "verdict": "unknown"}]}) + "\n")
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps([
        {"trial_id": "x", "claim_id": "a", "human": "true", "split": "validate"},
        {"trial_id": "x", "claim_id": "b", "human": "true", "split": "validate"},
        {"trial_id": "x", "claim_id": "c", "human": "false", "split": "validate"},
        {"trial_id": "x", "claim_id": "a", "human": "", "split": "tune"},
    ]))
    out = calibrate(judged, labels)
    assert out["validate"] == {"n": 3, "agree": 1, "agreement_rate": 1 / 3, "judge_true_human_false": 0, "judge_false_human_true": 1, "judge_unknown": 1, "disagreements": [{"trial_id": "x", "claim_id": "b", "human": "true", "judge": "false"}]}
    assert out["tune"]["n"] == 0


def test_ingest_sheet_round_trip_and_refusals(tmp_path: Path) -> None:
    from billingops.evaluation.annotation import ingest_sheet

    labels = [
        {"trial_id": "t::A::0", "claim_id": "a", "polarity": "must", "split": "tune", "human": "", "note": ""},
        {"trial_id": "t::A::0", "claim_id": "b", "polarity": "must_not", "split": "validate", "human": "", "note": ""},
    ]
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(json.dumps(labels), encoding="utf-8")
    sheet = tmp_path / "sheet.md"
    header = "## 1. `t::A::0`  (tune, duplicate, A, refund_proposed → closed)\n\n| claim_id | polarity | 断言 | human | 判断理由 |\n|---|---|---|---|---|\n"
    # A sheet that misses a claim writes nothing.
    sheet.write_text(header + "| `a` | must | says X | true | quote |\n", encoding="utf-8")
    partial = ingest_sheet(sheet, labels_path, annotated_by="ann", provenance="assisted")
    assert partial["written"] == 0 and partial["missing_count"] == 1
    assert json.loads(labels_path.read_text())[0]["human"] == ""
    # An unparseable verdict is reported, not coerced.
    sheet.write_text(header + "| `a` | must | says X | yes | quote |\n| `b` | must_not | says Y | false | quote |\n", encoding="utf-8")
    bad = ingest_sheet(sheet, labels_path, annotated_by="ann", provenance="assisted")
    assert bad["written"] == 0 and "yes" in bad["problems"][0]
    # A complete sheet writes every verdict, keeps a blank backup and records provenance.
    sheet.write_text(header + "| `a` | must | says X | true | because … |\n| `b` | must_not | says Y | false | because … |\n", encoding="utf-8")
    ok = ingest_sheet(sheet, labels_path, annotated_by="ann", provenance="assisted: reviewed pre-annotation")
    assert ok["written"] == 2 and ok["verdicts"] == {"true": 1, "false": 1, "unknown": 0}
    written = json.loads(labels_path.read_text())
    assert written[0]["human"] == "true" and written[0]["annotation_provenance"].startswith("assisted")
    assert json.loads((tmp_path / "labels.blank.json").read_text())[0]["human"] == ""
