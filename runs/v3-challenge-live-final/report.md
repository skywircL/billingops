# BillingOps experiment report: challenge_cases_v1-openai

Model: `deepseek-flash`. Runs: 16 (8 paired cases, 8 template families).
live model, no local reuse or replay
Evaluable pairs: 8/8; excluded incomplete/unscorable pairs: 0. Costs and latency retain all runs.

## Primary metric: overall case success (paired A/B)

| | A fixed workflow | B dynamic investigation | B − A |
|---|---|---|---|
| success rate (n=8) | 87.5% | 100.0% | +0.125 |

Family bootstrap: exploratory 95% percentile interval from 2000 family-level resamples over 8 families. Interval for B − A: [+0.000, +0.375].

## Stratified results (each stratum with its own n; no borrowing of the overall sample size)

| stratum | n | families | A | B | B − A |
|---|---|---|---|---|---|
| duplicate | 6 | 6 | 100.0% | 100.0% | +0.000 |
| refund_status | 1 | 1 | 100.0% | 100.0% | +0.000 |
| subscription | 1 | 1 | 0.0% | 100.0% | +1.000 |

## Secondary metrics per group

| metric | A | B |
|---|---|---|
| mean cost per case (USD, all model calls) | 0.0013 | 0.0057 |
| total cost (USD) | 0.01 | 0.05 |
| mean model calls | 2.00 | 7.25 |
| mean reads | 3.12 | 3.88 |
| mean clarifications | 0.38 | 0.00 |
| mean latency (s, simulated humans answer instantly) | 6.56 | 22.68 |
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
| mean cost (USD) | 0.0014 | 0.0067 |
| mean reads | 3.83 | 4.33 |
| mean model calls | 2.00 | 7.67 |

## Failure analysis

- `challenge-price-credit-wording` [A] challenge_price_credit_wording: state=closed, conclusion=explain_refund_status, failed checks=['conclusion', 'facts', 'final_state']

## Appendix

McNemar exact test on discordant pairs: A-only successes 0, B-only successes 1, p = 1.000. Assumption: pairs are independent; violated when several cases share a family.

Sensitivity analysis excluding infrastructure failures (0 pairs excluded; rule: exclude a pair when either side failed on Billing or model-API infrastructure (5xx, connection, timeout, rate limit). Budget exhaustion, protocol repairs and software errors are never excluded.): A 87.5%, B 100.0%, B − A +0.125 over n=8.

No non-inferiority test was run. A non-significant difference is not evidence of equal performance.
