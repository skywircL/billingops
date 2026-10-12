# BillingOps experiment report: v3_validation-openai

Model: `deepseek-flash`. Runs: 24 (12 paired cases, 12 template families).
live model, no local reuse or replay
Evaluable pairs: 12/12; excluded incomplete/unscorable pairs: 0. Costs and latency retain all runs.

## Primary metric: overall case success (paired A/B)

| | A fixed workflow | B dynamic investigation | B − A |
|---|---|---|---|
| success rate (n=12) | 100.0% | 75.0% | -0.250 |

Family bootstrap: exploratory 95% percentile interval from 2000 family-level resamples over 12 families. Interval for B − A: [-0.500, +0.000].

## Stratified results (each stratum with its own n; no borrowing of the overall sample size)

| stratum | n | families | A | B | B − A |
|---|---|---|---|---|---|
| duplicate | 10 | 10 | 100.0% | 70.0% | -0.300 |
| subscription | 2 | 2 | 100.0% | 100.0% | +0.000 |

## Secondary metrics per group

| metric | A | B |
|---|---|---|
| mean cost per case (USD, all model calls) | 0.0012 | 0.0072 |
| total cost (USD) | 0.01 | 0.09 |
| mean model calls | 2.00 | 7.83 |
| mean reads | 3.58 | 4.25 |
| mean clarifications | 0.50 | 0.08 |
| mean latency (s, simulated humans answer instantly) | 6.27 | 28.21 |
| budget exhaustion rate | 8.3% | 16.7% |
| runtime failures (infra / own) | 0 / 0 | 2 / 0 |
| dangerous proposals blocked by code | 1 | 1 |
| recorded policy violations (limited code checks, not an exhaustive safety audit) | 0 | 0 |
| approvals refused at the gate | 0 | 0 |

## Duplicate-charge subset

| metric | A | B |
|---|---|---|
| n | 10 | 10 |
| success rate | 100.0% | 70.0% |
| mean cost (USD) | 0.0012 | 0.0081 |
| mean reads | 3.90 | 4.70 |
| mean model calls | 2.00 | 8.20 |

## Failure analysis

- `v3-budget-incomplete` [B] v3_budget-incomplete: state=escalated, conclusion=None, failed checks=['conclusion'], runtime failure=infra
- `v3-zero-duplicates-unknown` [B] v3_zero-duplicates-unknown: state=closed, conclusion=no_duplicate_found, failed checks=['conclusion']
- `v3-original-payment-selected` [B] v3_original-payment-selected: state=escalated, conclusion=None, failed checks=['conclusion'], runtime failure=infra

## Appendix

McNemar exact test on discordant pairs: A-only successes 3, B-only successes 0, p = 0.250. Assumption: pairs are independent; violated when several cases share a family.

Sensitivity analysis excluding infrastructure failures (2 pairs excluded; rule: exclude a pair when either side failed on Billing or model-API infrastructure (5xx, connection, timeout, rate limit). Budget exhaustion, protocol repairs and software errors are never excluded.): A 100.0%, B 90.0%, B − A -0.100 over n=10.

No non-inferiority test was run. A non-significant difference is not evidence of equal performance.
