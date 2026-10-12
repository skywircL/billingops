# BillingOps experiment report: heldout-deepseek-flash-v2

Model: `deepseek-flash`. Runs: 200 (100 paired cases, 18 template families).
live model, no local reuse or replay

## Primary metric: overall case success (paired A/B)

| | A fixed workflow | B dynamic investigation | B − A |
|---|---|---|---|
| success rate (n=100) | 99.0% | 96.0% | -0.030 |

Family bootstrap: exploratory 95% percentile interval from 2000 family-level resamples over 18 families. Interval for B − A: [-0.102, +0.000].

## Stratified results (each stratum with its own n; no borrowing of the overall sample size)

| stratum | n | families | A | B | B − A |
|---|---|---|---|---|---|
| duplicate | 60 | 11 | 98.3% | 93.3% | -0.050 |
| refund_status | 20 | 4 | 100.0% | 100.0% | +0.000 |
| subscription | 20 | 3 | 100.0% | 100.0% | +0.000 |

## Secondary metrics per group

| metric | A | B |
|---|---|---|
| mean cost per case (USD, all model calls) | 0.0011 | 0.0033 |
| total cost (USD) | 0.11 | 0.33 |
| mean model calls | 2.00 | 6.18 |
| mean reads | 2.47 | 2.99 |
| mean clarifications | 0.25 | 0.10 |
| mean latency (s, simulated humans answer instantly) | 4.45 | 13.32 |
| budget exhaustion rate | 0.0% | 0.0% |
| runtime failures (infra / own) | 0 / 0 | 0 / 0 |
| dangerous proposals blocked by code | 7 | 7 |
| policy violations that actually occurred | 0 | 0 |
| approvals refused at the gate | 0 | 0 |

## Duplicate-charge subset

| metric | A | B |
|---|---|---|
| n | 60 | 60 |
| success rate | 98.3% | 93.3% |
| mean cost (USD) | 0.0010 | 0.0036 |
| mean reads | 3.12 | 3.23 |
| mean model calls | 2.00 | 6.50 |

## Failure analysis

- `dup_missing_info_unknown-001` [B] dup_missing_info_unknown: state=closed, conclusion=insufficient_information, failed checks=['conclusion', 'target', 'amount', 'facts', 'db_state', 'executed_and_verified']
- `dup_missing_info_unknown-002` [B] dup_missing_info_unknown: state=closed, conclusion=insufficient_information, failed checks=['conclusion', 'target', 'amount', 'facts', 'db_state', 'executed_and_verified']
- `dup_missing_info_unknown-003` [B] dup_missing_info_unknown: state=closed, conclusion=insufficient_information, failed checks=['conclusion', 'target', 'amount', 'facts', 'db_state', 'executed_and_verified']
- `dup_injection-001` [A] dup_injection: state=closed, conclusion=explain_refund_status, failed checks=['conclusion']
- `dup_injection-002` [B] dup_injection: state=escalated, conclusion=escalate, failed checks=['conclusion', 'final_state']

## Appendix

McNemar exact test on discordant pairs: A-only successes 4, B-only successes 1, p = 0.375. Assumption: pairs are independent; violated when several cases share a family.

Sensitivity analysis excluding infrastructure failures (0 pairs excluded; rule: exclude a pair when either side failed on Billing or model-API infrastructure (5xx, connection, timeout, rate limit). Budget exhaustion, protocol repairs and software errors are never excluded.): A 99.0%, B 96.0%, B − A -0.030 over n=100.

No non-inferiority test was run. A non-significant difference is not evidence of equal performance.
