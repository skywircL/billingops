# D1 decisions (fixed)

These are the choices the plan asks to freeze on day one. Any change to them
after the freeze is a versioned change that requires a full rerun of the
primary experiment.

## Upstream pin

`vinimabreu/langgraph-production` at commit `89d9079e6608ddc50d5a086f64b2f70b80b3ba30`
(2026-08-04), vendored under `vendor/langgraph-production/` with its test
suite (373 tests, reproduced on this machine). Reused as is: SQLite
checkpointing pattern (`SqliteSaver`, `durability="sync"`), the effect ledger
(`SqliteEffectStore`: claim-before-act, append-only executions table), the
approval ledger and token minting (`mint_token`, `authorize`,
`SqliteApprovalLedger`), `digest`/`canonical_json`, and the crash-injection
hooks (`CrashInjector`, `SimulatedCrash`). Everything else is this project.

## Model pin

DeepSeek V4.1 Flash, served as `deepseek-flash` on DeepSeek's OpenAI-compatible
endpoint (confirmed against `GET /models` and the published pricing page,
which lists `deepseek-flash` = DeepSeek-V4.1-Flash). Configured in `.env`
(see `.env.example`). The project has no other real-model adapter.

| item | value |
|---|---|
| model id | `deepseek-flash` (`BILLINGOPS_LLM_MODEL`) |
| endpoint | `https://api.deepseek.com/v1` (`BILLINGOPS_LLM_BASE_URL`) |
| SDK | `openai` 3.16.2 (Python), `chat.completions` |
| adapter | `billingops/llm/openai_compat.py` (`BILLINGOPS_MODEL=openai`) |
| tool calling | native `tools` (function schemas), `tool_choice="auto"`, `parallel_tool_calls=false`; `reasoning_content` is carried back with each assistant turn |
| sampling | `temperature=0`, `max_tokens=4096` (`BILLINGOPS_LLM_MAX_TOKENS`) |
| SDK retries | 2 (model calls only; the refund write has zero retries at every layer) |
| prices used for cost reporting | peak-hour list prices: $0.30 / $1.20 per MTok in/out, $0.006 cache hit (`BILLINGOPS_PRICE_*`) |

Verified on 2026-09-20: all seven demonstrations (`scripts/demo.py --model openai`),
the three replay traces under `data/replays/real/`, and the dev-set regression.

Observed: the endpoint is not strictly deterministic at temperature 0. The
same ambiguous case ("charged twice", two invoices each paid twice) produced a
clarification on most runs and a direct proposal on one; wording of the
question varied between runs. The experiment reports single runs and does not
claim determinism.

Other pinned versions: Python 3.13, langgraph 1.2.11, langgraph-checkpoint-sqlite 3.1.1,
fastapi 0.141.1, pydantic 2.13.5, httpx 0.28.1, openai 3.16.2. See `uv.lock`.

## Budgets per case (cumulative across pauses and restarts)

8 reads, 2 clarifications, 14 investigation-model calls, 2 protocol/format
repairs in total. A repair also counts as an investigation call. Extraction and
conclusion generation are separate calls, counted in cost but not in the 14.
On exhaustion the evidence is preserved and the case is escalated.

## Business state machine

`investigating → awaiting_user → investigating → awaiting_approval → executing → verifying → closed`
with `uncertain`, `error`, `escalated` as documented in the plan. The business
state says who is handling the case; the effect ledger says what happened to
the refund. The two are shown side by side and neither overwrites the other.
Allowed commands per state are in `billingops/cases/store.py:ALLOWED_COMMANDS`.

Transient detail: while the executor runs the gate and the pre-execution
revalidation after an approval, the case reads `investigating`; it moves to
`executing` only when the claim is about to be taken.

## Dispute key and amount rules

Dispute key: `customer_id:duplicate_charge:<extra payment id>`, built server-side
from validated Billing records. The model cannot supply it.

Amount rules (`billingops/money.py`): an invoice is duplicately charged when
more than one **succeeded** payment is attached to it; payments are ordered by
`paid_at, id`; the first paid the invoice, later ones are extra; refundable =
extra payment amount − refunds already recorded (`pending` or `completed`); an
unsettled extra payment is not executable; invoice and payment must belong to
the case's customer; refunds must have been read before an amount exists.

## Proposal fingerprint and approval binding

`fingerprint = digest(proposal_version, customer, payment, invoice, amount, currency, dispute key, versions of the invoice/payments/refunds records)`.
`action_id = digest(case_id, fingerprint)`; the Billing idempotency key is
`case_id:action_id`. The approval token is upstream's
`mint_token(thread=case, ticket=case, action_id, state_digest=fingerprint)`, so
a changed proposal (new version) has a new token and the old approval is dead.
The API additionally refuses a decision whose `proposal_version` is not current.

## Scoring specification

See `docs/EXPERIMENT.md`.

## Evaluation v4 decisions (2026-09-21/22)

* Reference execution never runs the fixed rules: a task's `reference_path`
  drives a `PathModel` in dynamic mode, so the sealed set is not filtered by
  what rule set A can do.
* Required evidence is stated as facts (`invoice_owned`, `refunds_of_payment`,
  …) and resolved to any read that establishes them; tool sequences are never
  graded.
* The gate judges group A only and reports an interval: `S_lower` (unknown
  and missing trials count as failures) and `S_upper`; one unknown trial does
  not make a batch undecidable.
* Prefix diagnostics verify the environment side (tool results) and never fall
  back to the live model inside the prefix; a mismatch is `unreproducible`.
* Failure classes come from exception types, events and HTTP statuses; the
  old `INFRA_MARKERS` substring list is not used by the v4 runner.
* Trial artifacts are named per trial index; the calibration run
  `runs/calibration-v1-deepseek-flash` predates that fix and lacks t1/t2
  artifacts (recorded in EVALUATION_V4.md).
