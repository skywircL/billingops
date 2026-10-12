from __future__ import annotations

import json
from pathlib import Path

import pytest

from billingops.evaluation.artifacts import file_digest, load_trial, read_legacy_db
from billingops.evaluation.data import generate_dev
from billingops.evaluation.experiment import run_case, run_experiment
from billingops.evaluation.rescore import grade, process_checks, refunds_from_world, summarise_grades
from billingops.evaluation.review import masked_packet, selection
from billingops.evaluation.stats import paired_resource_differences
from test_evaluation import TRUTH, EVIDENCE, _view


def test_missing_world_is_not_reconstructed_from_an_app_receipt():
    view = _view("closed", "refund_proposed", {"payment_id": "PAY-2", "amount_cents": 4900}, EVIDENCE, {"status": "done", "receipt": {"amount_cents": 4900}})
    r = grade(TRUTH, view, None, {"checks": {"db_state": True}})
    assert r["status"] == "unscorable"
    assert r["checks"]["db_state"] == {"value": True, "provenance": "legacy"}
    view["case"]["proposal"]["amount_cents"] = 1
    assert grade(TRUTH, view, None)["status"] == "fail"


def test_grader_errors_are_not_agent_failures():
    assert grade({}, {"case": {}}, [])["reason"] == "grader_error"
    assert grade(TRUTH, None, None)["reason"] == "missing_application_artifact"


def test_exclusion_is_paired_but_cost_keeps_unscorable_trials():
    originals = [dict(case_id="c", group=g, family="f", reads=1 if g=="A" else 9, cost_usd=1 if g=="A" else 7, latency_s=2, success=g=="A") for g in "AB"]
    rows = [{**r, "status": "pass" if r["group"]=="A" else "unscorable", "reason": "grader_error"} for r in originals]
    summary = summarise_grades(rows, originals)
    assert summary["evaluable_pairs"] == 0 and summary["total_case_ids"] == 1
    assert summary["resources_all_runs"]["cost_usd"]["mean_difference"] == 6
    assert summary["resources_all_runs"]["cost_usd"]["n"] == 1


def test_sampling_is_order_independent_and_deduplicates():
    rows = []
    for category, n in (("duplicate", 12), ("refund_status", 4), ("subscription", 4)):
        for i in range(n):
            for group in "AB":
                rows.append(dict(case_id=f"{category}-{i}", category=category, group=group, success=not (category=="duplicate" and i==0 and group=="B")))
    a,b=selection(rows),selection(list(reversed(rows)))
    assert a == b
    assert len(a["random_sample"]) == 10
    assert len({x["case_id"] for x in a["selected"]}) == len(a["selected"])
    assert next(x for x in a["selected"] if x["case_id"] == "duplicate-0")["reasons"][0] == "discordant_pair"


def test_new_trial_archives_world_and_can_be_fully_rescored(billing, tmp_path):
    from dataclasses import asdict
    case = next(c for c in generate_dev() if c.family == "dup_clean")
    workdir=tmp_path / "dbs"
    result = asdict(run_case(case, "A", model_spec="fake", billing=billing, workdir=workdir))
    assert result["score_status"] == "pass"
    trial = load_trial(tmp_path, result, {})
    assert trial["billing_final"]["refund_requests"]
    assert grade(case.ground_truth, trial["view"], refunds_from_world(trial["billing_final"], trial["task"], trial["view"]))["status"] == "pass"
    assert process_checks(trial)["verification_before_refund_close"] is True
    source_path=workdir / f"{case.case_id}-A.sqlite3"
    import sqlite3
    path=tmp_path / "closed-archive.sqlite3"
    source=sqlite3.connect(source_path.resolve().as_uri()+"?mode=ro",uri=True)
    destination=sqlite3.connect(path)
    source.backup(destination)
    source.close(); destination.close()
    before=file_digest(path)
    legacy=read_legacy_db(path)
    assert file_digest(path)==before and legacy["view"]["case"]["state"]=="closed"
    packet=masked_packet(trial,"P01-X")
    assert not {"group","mode","original_result","source_db"}.intersection(packet)
    assert packet["review"]["status"]=="pending"
    # Mutating a bundle is detected rather than quietly rescored.
    artifact=tmp_path / result["artifact"]["path"]
    saved=json.loads(artifact.read_text());saved["billing_final"]["refunds"]=[]
    artifact.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="digest mismatch"):
        load_trial(tmp_path,result,{})


def test_grader_exception_does_not_abort_following_runs(billing,tmp_path,monkeypatch):
    from billingops.evaluation import rescore
    def broken(**kwargs):
        raise RuntimeError("injected scorer fault")
    monkeypatch.setattr(rescore,"score_case",broken)
    case=next(c for c in generate_dev() if c.family=="dup_clean")
    run_experiment([case],groups=("A","B"),model_spec="fake",billing=billing,out_dir=tmp_path/"run",label="grader-error-test")
    rows=[json.loads(s) for s in (tmp_path/"run/results.jsonl").read_text().splitlines()]
    assert len(rows)==2 and all(r["score_status"]=="unscorable" and r["success"] is None for r in rows)
    assert all(r["score_reason"]=="grader_error" and r["final_state"]=="closed" for r in rows)
    with pytest.raises(FileExistsError):
        run_experiment([case],groups=("A",),model_spec="fake",billing=billing,out_dir=tmp_path/"run",label="must-not-overwrite")


def test_paired_resource_statistics_preserve_sign_and_pairing():
    rows=[dict(case_id=f"c{i}",group=g,family=f"f{i}",reads=i+(g=="B"),cost_usd=1+2*(g=="B"),latency_s=4-(g=="B")) for i in range(10) for g in "AB"]
    r=paired_resource_differences(rows,iterations=100)
    assert r["reads"]["mean_difference"]==1
    assert r["cost_usd"]["mean_difference"]==2
    assert r["latency_s"]["mean_difference"]==-1


def test_challenge_ambiguity_accepts_safe_alternatives_but_not_a_refund():
    from billingops.evaluation.challenges import build_challenges
    task=next(c for c in build_challenges() if c.family=="challenge_ambiguous_target")
    for state,conclusion in (("closed","insufficient_information"),("escalated","escalate")):
        view=_view(state,conclusion,None,[],None)
        assert grade(task.ground_truth,view,[])["status"]=="pass"
    view=_view("closed","refund_proposed",{"payment_id":"PAY-88213","amount_cents":4900},[],{"status":"done"})
    assert grade(task.ground_truth,view,[{"status":"completed","amount_cents":4900}])["status"]=="fail"


def test_legacy_rubric_does_not_gain_alternative_end_states():
    view=_view("escalated","refund_proposed",{"payment_id":"PAY-2","amount_cents":4900},EVIDENCE,{"status":"done"})
    assert grade(TRUTH,view,[{"status":"completed","amount_cents":4900}])["checks"]["final_state"]["value"] is False


def test_artifact_failure_is_reported_without_losing_the_business_result(billing,tmp_path,monkeypatch):
    from billingops.evaluation import artifacts
    def failed_write(*args,**kwargs):
        raise OSError("injected archive write failure")
    monkeypatch.setattr(artifacts,"write_json",failed_write)
    case=next(c for c in generate_dev() if c.family=="dup_clean")
    result=run_case(case,"A",model_spec="fake",billing=billing,workdir=tmp_path/"dbs")
    assert result.success is True and result.final_state=="closed"
    assert result.artifact["sha256"] is None
    assert result.artifact["errors"]==["artifact_write:OSError"]
