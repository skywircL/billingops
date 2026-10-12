# BillingOps experiment report: v3_validation-fake

Model: `fake`. Runs: 24 (12 paired cases, 12 template families).
scripted stand-in: harness validation only, not evidence about any model
Evaluable pairs: 12/12; excluded incomplete/unscorable pairs: 0. Costs and latency retain all runs.

## Primary metric: overall case success (paired A/B)

| | A fixed workflow | B dynamic investigation | B − A |
|---|---|---|---|
| success rate (n=12) | 100.0% | 100.0% | +0.000 |

Family bootstrap: descriptive only: the interval collapsed to a point over 12 families (no variation in the paired difference).

## Stratified results (each stratum with its own n; no borrowing of the overall sample size)

| stratum | n | families | A | B | B − A |
|---|---|---|---|---|---|
| duplicate | 10 | 10 | 100.0% | 100.0% | +0.000 |
| subscription | 2 | 2 | 100.0% | 100.0% | +0.000 |

## Secondary metrics per group

| metric | A | B |
|---|---|---|
| mean cost per case (USD, all model calls) | 0.0003 | 0.0014 |
| total cost (USD) | 0.00 | 0.02 |
| mean model calls | 2.00 | 7.17 |
| mean reads | 3.58 | 3.58 |
| mean clarifications | 0.50 | 0.50 |
| mean latency (s, simulated humans answer instantly) | 0.06 | 0.08 |
| budget exhaustion rate | 8.3% | 8.3% |
| runtime failures (infra / own) | 0 / 0 | 0 / 0 |
| dangerous proposals blocked by code | 1 | 1 |
| recorded policy violations (limited code checks, not an exhaustive safety audit) | 0 | 0 |
| approvals refused at the gate | 0 | 0 |

## Duplicate-charge subset

| metric | A | B |
|---|---|---|
| n | 10 | 10 |
| success rate | 100.0% | 100.0% |
| mean cost (USD) | 0.0003 | 0.0016 |
| mean reads | 3.90 | 3.90 |
| mean model calls | 2.00 | 7.60 |

## Failure analysis

No failed runs.

## Appendix

McNemar exact test on discordant pairs: A-only successes 0, B-only successes 0, p = 1.000. Assumption: pairs are independent; violated when several cases share a family.

Sensitivity analysis excluding infrastructure failures (0 pairs excluded; rule: exclude a pair when either side failed on Billing or model-API infrastructure (5xx, connection, timeout, rate limit). Budget exhaustion, protocol repairs and software errors are never excluded.): A 100.0%, B 100.0%, B − A +0.000 over n=12.

No non-inferiority test was run. A non-significant difference is not evidence of equal performance.
