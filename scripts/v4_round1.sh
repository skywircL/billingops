#!/bin/zsh
# Post-processing of the v4 round-1 formal batch: judge, case import/mining, gate, Langfuse read-back,
# the three closed-loop demonstrations and the static case desk. Re-runnable: imports and mining are idempotent.
set -e
cd "$(dirname "$0")/.."
P=.venv/bin/python
DB=data/cases.sqlite3
OUT=analysis/v4-round1
mkdir -p $OUT
S=runs/v4-sealed-v1-deepseek-flash; R=runs/v4-regression-v1-deepseek-flash; C=runs/v4-canary-v1-deepseek-flash

echo "== response layer (auxiliary): first trial of every sealed (task, group)"
$P -m billingops.evaluation judge run --run $S --tasks data/tasks/sealed-v1.json --trial-index 0 > $OUT/judge-sealed-t0.json

echo "== rescore under the current grader (g2: equivalent outcomes carry their own state expectation); as-run scores stay in place"
for r in $C $R $S; do
  [ -d $r/rescore-g2 ] || $P -m billingops.evaluation suite rescore --run $r > /dev/null
  $P - "$r" <<'PY'
import json,sys; m=json.load(open(sys.argv[1]+"/rescore-g2/manifest.json"))["rescore"]; print(sys.argv[1], {k:m[k] for k in ("rows","status_changed","artifact_unavailable","task_world_changed")})
PY
done

echo "== case flywheel: import (rescored) + mine"
for r in $C $R $S; do
  $P -m billingops.evaluation cases import --run $r --results-name rescore-g2 --db $DB > /dev/null
  $P -m billingops.evaluation cases mine --run $r --db $DB
done

echo "== gate"
# regression-v1 (frozen and already run) contains dup_injection-001/002, which were still open in v3.1; they are reported, not gated.
MUST="dup_clean-002,dup_missing_info_unknown-001,dup_missing_info_unknown-002,dup_missing_info_unknown-003,challenge-noisy-unique,challenge-price-credit-wording,challenge-payment-multihop,challenge-tool-result-injection,v3-corrected-identifier,v3-three-payments-selected,v3-credit-negated,v3-credit-paraphrase"
$P -m billingops.evaluation release-check --sealed-run $S --regression-run $R --regression-must-pass "$MUST" --results-name rescore-g2 --out $OUT/release.json > /dev/null
$P -m billingops.evaluation release-check --sealed-run $S --regression-run $R --regression-must-pass "$MUST" --out $OUT/release-as-run-g1.json > /dev/null
$P - <<'PY'
import json; r=json.load(open("analysis/v4-round1/release.json"))
print("decision:", r["decision"], "| S:", r["S"], "| blockers:", r["blockers"], "| reasons:", r["reasons"])
print("slices:", {k:(round(v["s_lower"],3), round(v["s_upper"],3)) for k,v in r["slices"].items()})
print("B:", {k: r["groups"]["B"][k] for k in ("s_lower","s_upper")}, "| paired:", {k:v for k,v in (r["paired_ab"] or {}).items() if k in ("n","mean_a","mean_b","diff")})
print("regression:", r["regression"])
PY

echo "== Langfuse read-back (verified only when the server returns the trace)"
$P -m billingops.evaluation cases langfuse-sync --db $DB --limit 300

echo "== chain 1 (badcase): challenge-noisy-unique — legacy failure -> v3.1 fix -> repeated regression"
F1=$($P -m billingops.evaluation cases add --db $DB --kind badcase --channel historical --task challenge-noisy-unique --run runs/challenge-v1-live \
  --title "B could not identify the unique duplicate from noisy statements (challenge-v1 live, 2026-09-20)" --by ops-dana --group B \
  --evidence '{"source": "analysis/challenge-review/report.md", "fixed_in": "v3.1 (investigation/target.py account-scope resolution)"}' \
  --hypothesis "target selection depended on the model's own proposal instead of customer-scope candidates" --attribution agent | $P -c "import json,sys; print(json.load(sys.stdin)['finding_id'])")
$P -m billingops.evaluation cases review $F1 --db $DB --by ops-dana --action triage --status fixed --attribution agent --note "fix landed in v3.1; protected by regression-v1" > /dev/null
$P -m billingops.evaluation cases link $F1 --db $DB --by ops-dana --key fix_version --value v3.1 > /dev/null
$P -m billingops.evaluation cases link $F1 --db $DB --by ops-dana --key retest_run --value $R > /dev/null
$P -m billingops.evaluation cases promote $F1 --db $DB --by ops-dana --to regression --note "kept as must-pass" > /dev/null
echo "finding $F1 -> fixed, linked to v3.1 and $R, promoted to regression"

echo "== chain 2 (goodcase): dup_clean-002 — legal alternative path kept as a quality reference"
F2=$($P -m billingops.evaluation cases list --db $DB --task dup_clean-002 --kind goodcase | $P -c "
import json,sys; rows=json.load(sys.stdin)
alt=[r for r in rows if r['channel']=='targeted_alternative_path'] or [r for r in rows if r['channel']=='targeted_stable'] or rows
print(alt[0]['finding_id'] if alt else '')")
if [ -n "$F2" ]; then
  $P -m billingops.evaluation cases review $F2 --db $DB --by ops-dana --action confirm --status confirmed --note "same outcome, fewer reads; execution still pre-checked and verified" > /dev/null
  $P -m billingops.evaluation cases promote $F2 --db $DB --by ops-dana --to quality_reference --note "capability to preserve" > /dev/null
  echo "finding $F2 -> confirmed, promoted to quality_reference"
else
  echo "no goodcase mined for dup_clean-002 in this round (recorded as-is)"
fi

echo "== chain 3 (rubric): v3-original-payment-selected / v3-zero-duplicates-unknown — spec 7.4 revision -> rubric v2 -> re-run"
F3=$($P -m billingops.evaluation cases add --db $DB --kind dispute --channel scoring_dispute --task v3-original-payment-selected --run runs/v3.1-validation-live \
  --title "gold accepted only insufficient_information; BUSINESS_SPEC 4.1 rule 1 / 7.4 also accept no_refund_due (target_is_not_an_extra_payment)" --by ops-dana \
  --evidence '{"spec": "docs/BUSINESS_SPEC.md#7.4", "revised_task": "v3-original-payment-selected--rubric-v2", "dataset": "regression-v1.1"}' --attribution grader | $P -c "import json,sys; print(json.load(sys.stdin)['finding_id'])")
$P -m billingops.evaluation cases review $F3 --db $DB --by ops-dana --action revise-spec --status confirmed --attribution grader --note "outcomes_any widened per spec 7.4; original score untouched" > /dev/null
$P -m billingops.evaluation suite run --tasks data/tasks/regression-v1.1.json --out runs/v4-regression-v1.1-rubric-deepseek-flash --groups A,B --trials 3 --model openai --workers 2 --budget-usd 0.3 \
  --task-ids "v3-original-payment-selected--rubric-v2,v3-zero-duplicates-unknown--rubric-v2,dup_injection-001--rubric-v2" --label v4-rubric-v2 > $OUT/rubric-v2-run.log
$P -m billingops.evaluation cases import --run runs/v4-regression-v1.1-rubric-deepseek-flash --db $DB > /dev/null
$P -m billingops.evaluation cases mine --run runs/v4-regression-v1.1-rubric-deepseek-flash --db $DB > /dev/null
$P -m billingops.evaluation cases link $F3 --db $DB --by ops-dana --key rescore_run --value runs/v4-regression-v1.1-rubric-deepseek-flash > /dev/null
$P - <<'PY'
import json
rows=[json.loads(l) for l in open("runs/v4-regression-v1.1-rubric-deepseek-flash/results.jsonl")]
for tid in sorted({r["task_id"] for r in rows}):
    for g in "AB":
        rs=[r for r in rows if r["task_id"]==tid and r["group"]==g]
        print(tid, g, [r["status"] for r in rs], [r["conclusion_type"] for r in rs])
PY

echo "== case desk export"
$P -m billingops.evaluation cases export --db $DB --out $OUT/cases
echo ROUND1_DONE
