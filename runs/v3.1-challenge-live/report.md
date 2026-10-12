# BillingOps experiment report: challenge_cases_v1-openai

Model: `deepseek-flash`. Runs: 16 (8 paired cases, 8 template families).
live model, no local reuse or replay
Evaluable pairs: 8/8; excluded incomplete/unscorable pairs: 0. Costs and latency retain all runs.

## Primary metric: overall case success (paired A/B)

| | A fixed workflow | B dynamic investigation | B − A |
|---|---|---|---|
| success rate (n=8) | 100.0% | 100.0% | +0.000 |

Family bootstrap: descriptive only: the interval collapsed to a point over 8 families (no variation in the paired difference).

## Stratified results (each stratum with its own n; no borrowing of the overall sample size)

| stratum | n | families | A | B | B − A |
|---|---|---|---|---|---|
| duplicate | 6 | 6 | 100.0% | 100.0% | +0.000 |
| refund_status | 1 | 1 | 100.0% | 100.0% | +0.000 |
| subscription | 1 | 1 | 100.0% | 100.0% | +0.000 |

## Secondary metrics per group

| metric | A | B |
|---|---|---|
| mean cost per case (USD, all model calls) | 0.0017 | 0.0038 |
| total cost (USD) | 0.01 | 0.03 |
| mean model calls | 2.00 | 6.38 |
| mean reads | 3.25 | 3.12 |
| mean clarifications | 0.38 | 0.12 |
| mean latency (s, simulated humans answer instantly) | 7.61 | 15.09 |
| budget exhaustion rate | 0.0% | 0.0% |
| runtime failures (infra / own) | 0 / 0 | 0 / 0 |
| dangerous proposals blocked by code | 0 | 0 |
| recorded policy violations (limited code checks, not an exhaustive safety audit) | 0 | 0 |
| approvals refused at the gate | 0 | 0 |

## Duplicate-charge subset

| metric | A | B |
|---|---|---|
| n | 6 | 6 |
| success rate | 100.0% | 100.0% |
| mean cost (USD) | 0.0019 | 0.0043 |
| mean reads | 3.83 | 3.33 |
| mean model calls | 2.00 | 6.67 |

## Failure analysis

No failed runs.

## Appendix

McNemar exact test on discordant pairs: A-only successes 0, B-only successes 0, p = 1.000. Assumption: pairs are independent; violated when several cases share a family.

Sensitivity analysis excluding infrastructure failures (0 pairs excluded; rule: exclude a pair when either side failed on Billing or model-API infrastructure (5xx, connection, timeout, rate limit). Budget exhaustion, protocol repairs and software errors are never excluded.): A 100.0%, B 100.0%, B − A +0.000 over n=8.

No non-inferiority test was run. A non-significant difference is not evidence of equal performance.
