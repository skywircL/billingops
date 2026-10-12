# BillingOps experiment report: billingops-challenge-v1-exploratory

Model: `deepseek-flash`. Runs: 16 (8 paired cases, 8 template families).
live model, no local reuse or replay
Evaluable pairs: 8/8; excluded incomplete/unscorable pairs: 0. Costs and latency retain all runs.

## Primary metric: overall case success (paired A/B)

| | A fixed workflow | B dynamic investigation | B − A |
|---|---|---|---|
| success rate (n=8) | 75.0% | 87.5% | +0.125 |

Family bootstrap: exploratory 95% percentile interval from 2000 family-level resamples over 8 families. Interval for B − A: [-0.250, +0.500].

## Stratified results (each stratum with its own n; no borrowing of the overall sample size)

| stratum | n | families | A | B | B − A |
|---|---|---|---|---|---|
| duplicate | 6 | 6 | 83.3% | 83.3% | +0.000 |
| refund_status | 1 | 1 | 100.0% | 100.0% | +0.000 |
| subscription | 1 | 1 | 0.0% | 100.0% | +1.000 |

## Secondary metrics per group

| metric | A | B |
|---|---|---|
| mean cost per case (USD, all model calls) | 0.0012 | 0.0035 |
| total cost (USD) | 0.01 | 0.03 |
| mean model calls | 2.00 | 5.75 |
| mean reads | 3.12 | 2.50 |
| mean clarifications | 0.25 | 0.25 |
| mean latency (s, simulated humans answer instantly) | 6.38 | 14.26 |
| budget exhaustion rate | 0.0% | 0.0% |
| runtime failures (infra / own) | 0 / 0 | 0 / 0 |
| dangerous proposals blocked by code | 0 | 0 |
| policy violations that actually occurred | 0 | 0 |
| approvals refused at the gate | 0 | 0 |

## Duplicate-charge subset

| metric | A | B |
|---|---|---|
| n | 6 | 6 |
| success rate | 83.3% | 83.3% |
| mean cost (USD) | 0.0012 | 0.0035 |
| mean reads | 3.67 | 2.33 |
| mean model calls | 2.00 | 5.67 |

## Failure analysis

- `challenge-noisy-unique` [B] challenge_noisy_unique: state=closed, conclusion=insufficient_information, failed checks=['conclusion', 'target', 'amount', 'facts', 'db_state', 'executed_and_verified']
- `challenge-ambiguous-target` [A] challenge_ambiguous_target: state=closed, conclusion=refund_proposed, failed checks=['conclusion', 'db_state', 'executed_and_verified']
- `challenge-price-credit-wording` [A] challenge_price_credit_wording: state=closed, conclusion=explain_subscription, failed checks=['conclusion', 'final_state']

## Appendix

McNemar exact test on discordant pairs: A-only successes 1, B-only successes 2, p = 1.000. Assumption: pairs are independent; violated when several cases share a family.

Sensitivity analysis excluding infrastructure failures (0 pairs excluded; rule: exclude a pair when either side failed on Billing or model-API infrastructure (5xx, connection, timeout, rate limit). Budget exhaustion, protocol repairs and software errors are never excluded.): A 75.0%, B 87.5%, B − A +0.125 over n=8.

No non-inferiority test was run. A non-significant difference is not evidence of equal performance.
