# BillingOps experiment report: heldout-fake

Model: `fake`. Runs: 200 (100 paired cases, 18 template families).
scripted stand-in: harness validation only, not evidence about any model

## Primary metric: overall case success (paired A/B)

| | A fixed workflow | B dynamic investigation | B − A |
|---|---|---|---|
| success rate (n=100) | 100.0% | 100.0% | +0.000 |

Family bootstrap: descriptive only: the interval collapsed to a point over 18 families (no variation in the paired difference).

## Stratified results (each stratum with its own n; no borrowing of the overall sample size)

| stratum | n | families | A | B | B − A |
|---|---|---|---|---|---|
| duplicate | 60 | 11 | 100.0% | 100.0% | +0.000 |
| refund_status | 20 | 4 | 100.0% | 100.0% | +0.000 |
| subscription | 20 | 3 | 100.0% | 100.0% | +0.000 |

## Secondary metrics per group

| metric | A | B |
|---|---|---|
| mean cost per case (USD, all model calls) | 0.0055 | 0.0178 |
| total cost (USD) | 0.55 | 1.78 |
| mean model calls | 2.00 | 5.74 |
| mean reads | 2.49 | 2.49 |
| mean clarifications | 0.25 | 0.25 |
| mean latency (s, simulated humans answer instantly) | 0.03 | 0.04 |
| budget exhaustion rate | 0.0% | 0.0% |
| runtime failures (infra / own) | 0 / 0 | 0 / 0 |
| dangerous proposals blocked by code | 7 | 7 |
| policy violations that actually occurred | 0 | 0 |
| approvals refused at the gate | 0 | 0 |

## Duplicate-charge subset

| metric | A | B |
|---|---|---|
| n | 60 | 60 |
| success rate | 100.0% | 100.0% |
| mean cost (USD) | 0.0058 | 0.0214 |
| mean reads | 3.15 | 3.15 |
| mean model calls | 2.00 | 6.57 |

## Failure analysis

No failed runs.

## Appendix

McNemar exact test on discordant pairs: A-only successes 0, B-only successes 0, p = 1.000. Assumption: pairs are independent; violated when several cases share a family.

Sensitivity analysis excluding infrastructure failures (0 pairs excluded; rule: exclude a pair when either side failed on Billing or model-API infrastructure (5xx, connection, timeout, rate limit). Budget exhaustion, protocol repairs and software errors are never excluded.): A 100.0%, B 100.0%, B − A +0.000 over n=100.

No non-inferiority test was run. A non-significant difference is not evidence of equal performance.
