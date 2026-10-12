# Replay traces

A trace is a JSON map from a normalised request digest to the recorded model
response. Normalisation renames tool-call ids `tc1, tc2, ...` in order of first
appearance (keeping the call/result correspondence), sorts JSON object keys, and
keeps message and array order. A replay miss raises immediately. Replay serves
only this project's adapter and message shape.

## `stand-in/` (present)

Recorded from the scripted stand-in so the replay machinery is exercised in the
test suite and can be demonstrated without a key. **They are not real model
trajectories and must not be presented as such.**

## `real/` (recorded 2026-09-20 with `deepseek-flash`, DeepSeek V4.1 Flash)

The three trajectories the plan requires, recorded from the pinned model and
listed in `real/manifest.json`. Each stops before the external write: no
approval and no refund POST is in any trace.

| trace | what it shows | entries |
|---|---|---|
| `direct_explanation` | refund-status question answered from the records, case closed | 6 |
| `clarification_then_investigation` | two invoices each paid twice → `ask_customer` which one → answer → proposal | 7 |
| `refund_proposal` | duplicate identified, extra payment named, proposal raised | 5 |

`tests/test_real_replays.py` replays all three in an isolated database and
asserts zero misses and the same end state. To re-record:

```bash
uv run python -m billingops.evaluation record --world duplicate_partial \
  --trace data/replays/real/direct_explanation.json \
  --text "Where is my refund for payment PAY-88213? It has not arrived."
uv run python -m billingops.evaluation record --world ambiguous \
  --trace data/replays/real/clarification_then_investigation.json \
  --text "You charged me twice, please refund one of them."
uv run python -m billingops.evaluation record \
  --trace data/replays/real/refund_proposal.json \
  --text "I was charged twice for invoice INV-2026-0142 (49.00 yuan each). Please refund the duplicate."
```

Replay a trace against a fresh, isolated database:

```bash
uv run python -m billingops.api --model replay:data/replays/real/refund_proposal.json --db /tmp/replay.sqlite3
```

Record/replay is never enabled during the formal experiment (`billingops.evaluation run` accepts only `fake` or `openai`).
