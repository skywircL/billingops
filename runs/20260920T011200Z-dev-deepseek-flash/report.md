# BillingOps experiment report: dev-deepseek-flash

Model: `deepseek-flash`. Runs: 40 (20 paired cases, 18 template families).
live model, no local reuse or replay

## Primary metric: overall case success (paired A/B)

| | A fixed workflow | B dynamic investigation | B − A |
|---|---|---|---|
| success rate (n=20) | 100.0% | 95.0% | -0.050 |

Family bootstrap: exploratory 95% percentile interval from 2000 family-level resamples over 18 families. Interval for B − A: [-0.158, +0.000].

## Stratified results (each stratum with its own n; no borrowing of the overall sample size)

| stratum | n | families | A | B | B − A |
|---|---|---|---|---|---|
| duplicate | 13 | 11 | 100.0% | 92.3% | -0.077 |
| refund_status | 4 | 4 | 100.0% | 100.0% | +0.000 |
| subscription | 3 | 3 | 100.0% | 100.0% | +0.000 |

## Secondary metrics per group

| metric | A | B |
|---|---|---|
| mean cost per case (USD, all model calls) | 0.0010 | 0.0033 |
| total cost (USD) | 0.02 | 0.07 |
| mean model calls | 1.95 | 6.20 |
| mean reads | 2.50 | 3.10 |
| mean clarifications | 0.30 | 0.05 |
| mean latency (s, simulated humans answer instantly) | 4.34 | 13.20 |
| budget exhaustion rate | 0.0% | 0.0% |
| runtime failures (infra / own) | 0 / 0 | 0 / 0 |
| dangerous proposals blocked by code | 2 | 2 |
| policy violations that actually occurred | 0 | 0 |
| approvals refused at the gate | 0 | 0 |

## Duplicate-charge subset

| metric | A | B |
|---|---|---|
| n | 13 | 13 |
| success rate | 100.0% | 92.3% |
| mean cost (USD) | 0.0011 | 0.0035 |
| mean reads | 3.08 | 3.15 |
| mean model calls | 2.00 | 6.38 |

## Failure analysis

- `dev-dup_other_customer-909` [B] dup_other_customer: state=closed, conclusion=no_duplicate_found, failed checks=['conclusion']

## Appendix

McNemar exact test on discordant pairs: A-only successes 1, B-only successes 0, p = 1.000. Assumption: pairs are independent; violated when several cases share a family.

Sensitivity analysis excluding infrastructure failures (0 pairs excluded; rule: exclude a pair when either side failed on Billing or model-API infrastructure (5xx, connection, timeout, rate limit). Budget exhaustion, protocol repairs and software errors are never excluded.): A 100.0%, B 95.0%, B − A -0.050 over n=20.

No non-inferiority test was run. A non-significant difference is not evidence of equal performance.
