# Architecture appendix

```
                 HTTP (customer / operator identities)                    HTTP
  browser ───►  billingops.api  ──► CaseStore (SQLite: cases, tasks,   ┌────────────────────┐
  (web/)        FastAPI          commands, idempotency, events)        │ billing_service    │
                                    │                                  │ FastAPI + SQLite   │
                                    ▼  tasks pending→running→done      │ BEGIN IMMEDIATE    │
                             CaseRuntime executor (one thread)          │ refund transaction │
                                    │                                  │ /refunds/by-key    │
                    ┌───────────────┴─────────────────┐                │ fault injection    │
                    ▼                                 ▼                └─────────▲──────────┘
        LangGraph case graph (checkpointed)   execution/refund.py               │
        extract → investigate ⇄ run_tool      claim (effect ledger) ────────────┤ one POST
                → clarify (interrupt)         record send → POST → in_doubt     │
                → conclude → gate (interrupt) verify by key ≤3 → done /         │ GET by key
                → precheck                    confirmed_not_executed / uncertain┘
```

Two databases, two processes. The application never touches the Billing file.

## Upstream contributions vs. this project

| upstream (`vendor/langgraph-production`) | this project (`src/billingops`, `src/billing_service`) |
|---|---|
| `SqliteSaver` checkpointing with `durability="sync"` | case graph, state, budgets, single-tool protocol, repairs |
| `SqliteEffectStore` claim-before-act, executions table | refund pipeline: claim → record send → POST once → verify by key |
| `mint_token` / `authorize` / approval ledger | proposal fingerprint, version binding, API-level version check |
| `digest`, `canonical_json` | dispute key, action id, idempotency scoping |
| `CrashInjector`, `SimulatedCrash` | crash points `refund:before_send`, `refund:after_send`, `run_tool:exit`, ... |
| — | Billing service, transaction, fault injection |
| — | persistent commands, `Idempotency-Key` scoping, single executor, startup recovery |
| — | evaluation data, simulators, scorer, paired statistics, report |
| — | Langfuse mirror (never on the execution path), operations page |

## The graph

Nodes: `extract`, `investigate`, `run_tool`, `fixed_investigate`, `clarify`,
`conclude`, `gate`, `precheck`. `tests/test_graph_structure.py` asserts that
the only edge into `precheck` comes from `gate`, that the only edges into
`gate` come from `conclude` (and the gate's own rejection loop), and that no
execution node exists in the graph at all. The refund POST lives in
`execution/refund.py`, reached only by the executor after the graph ended with
`outcome == "approved"`.

Group A (`mode=fixed`) replaces the `investigate ⇄ run_tool` loop with
`fixed_investigate`, which runs `investigation/rules.py` in a loop. Both groups
share `extract`, `clarify`, `conclude`, `gate`, `precheck`, budgets, the tool
runner, the amount code and execution.

## The single-tool protocol

The provider is asked for at most one tool call per turn
(`disable_parallel_tool_use`). The application enforces it again: two or more
tool calls in one turn execute nothing, every call gets an error result, and a
repair is charged. Prose with no tool call and an unknown tool are the other
two repair cases. A read past the read budget returns an error result without
consuming a read; a clarification past its budget does the same. When the
model-call or repair budget is exhausted the case escalates with its evidence.

## Commands, tasks, recovery

Every command-producing POST persists a command row and a task row before it
answers 202. The executor claims tasks serially with one `UPDATE ... WHERE
status='pending'` statement. On startup it takes a runtime lock (refusing to
run if another live executor holds it), then requeues tasks left `running`.

Recovery order in `CaseRuntime.handle_task`:

1. If the case has an action key and the ledger row is open (`sending`,
   `in_doubt`), nothing but verification is allowed, whatever the task was.
   A final row (`done`, `confirmed_not_executed`) only reconciles the case.
2. A command id already present in the checkpoint's `consumed_commands` is
   ignored and logged.
3. Otherwise the graph is started or resumed from its checkpoint; a `message`
   or `decision` resumes the pending interrupt, a `verify` on an `error` case
   resumes the graph.

## Execution and verification

`execute_approved`: claim the key in the effect ledger (payload `sending`);
if the claim was already taken, verify only. Otherwise append one row to the
executions table (the proof that a send window was opened), POST once with no
retries, store the raw response (or the exception) under `in_doubt`, then
verify by key up to three times. Verification outcomes: `matched_success` →
`done`, close; `terminal_rejection` → `confirmed_not_executed`, hand off;
`not_found` / `unavailable` / `mismatch` → keep `in_doubt`; after three, the
case is `uncertain`. Every later `/resume` performs exactly one query.
`/escalate` records operator, reason, time and the original action and leaves
the claim, approval, key and ledger untouched.

## Observability

`events` in the application database is the record. `observability/tracing.py`
mirrors graph events to Langfuse when `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`
are set and the `trace` extra is installed; every call is wrapped and a failing
tracer changes nothing (`tests/test_observability.py`). Human waiting time is
accumulated per case (`human_wait_seconds`) separately from run latency.
