"""Preregistered statistics for the paired A/B comparison.

* Paired difference in success rate (B minus A) over cases both groups ran.
* Bootstrap by template family, preserving pairing: families are resampled
  with replacement, the paired difference is recomputed over the cases of the
  resampled families. Reported as exploratory; with few families the interval
  is descriptive at best and the report says so.
* McNemar's exact test on discordant pairs, appendix only, with its
  independence assumption stated. Cases from one family are not independent.
* No non-inferiority test. A non-significant difference is not equivalence.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Any, Mapping, Sequence

MIN_FAMILIES_FOR_INTERVAL = 8


def paired_difference(pairs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """``pairs`` items carry ``a`` and ``b`` (bool success) and ``family``."""
    n = len(pairs)
    if n == 0:
        return {"n": 0, "rate_a": None, "rate_b": None, "diff": None}
    a = sum(1 for p in pairs if p["a"]) / n
    b = sum(1 for p in pairs if p["b"]) / n
    return {"n": n, "rate_a": a, "rate_b": b, "diff": b - a}


def bootstrap_by_family(pairs: Sequence[Mapping[str, Any]], *, iterations: int = 2000, seed: int = 7) -> dict[str, Any]:
    by_family: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for p in pairs:
        by_family[p["family"]].append(p)
    families = sorted(by_family)
    k = len(families)
    if k == 0:
        return {"families": 0, "interval": None, "degenerate": True, "note": "no data"}
    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(iterations):
        sample = [families[rng.randrange(k)] for _ in range(k)]
        chosen = [p for fam in sample for p in by_family[fam]]
        diffs.append(paired_difference(chosen)["diff"])
    diffs.sort()
    lo, hi = diffs[int(0.025 * iterations)], diffs[int(0.975 * iterations) - 1]
    too_few = k < MIN_FAMILIES_FOR_INTERVAL
    collapsed = lo == hi
    if too_few:
        note = f"descriptive only: {k} template families is fewer than the {MIN_FAMILIES_FOR_INTERVAL} needed for an interval"
    elif collapsed:
        note = f"descriptive only: the interval collapsed to a point over {k} families (no variation in the paired difference)"
    else:
        note = f"exploratory 95% percentile interval from {iterations} family-level resamples over {k} families"
    return {"families": k, "interval": [lo, hi], "degenerate": too_few or collapsed, "note": note}


def mcnemar_exact(pairs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    b_only = sum(1 for p in pairs if p["b"] and not p["a"])
    a_only = sum(1 for p in pairs if p["a"] and not p["b"])
    n = a_only + b_only
    if n == 0:
        return {"a_only": 0, "b_only": 0, "p_value": 1.0, "assumption": "pairs are independent; violated when several cases share a family"}
    k = min(a_only, b_only)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2**n)
    p = min(1.0, 2 * tail)
    return {"a_only": a_only, "b_only": b_only, "p_value": p, "assumption": "pairs are independent; violated when several cases share a family"}


def stratified(pairs: Sequence[Mapping[str, Any]], key: str = "category") -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for p in pairs:
        groups[p[key]].append(p)
    return {name: {**paired_difference(items), "families": len({p["family"] for p in items})} for name, items in sorted(groups.items())}


def mean(values: Sequence[float]) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def paired_resource_differences(rows: Sequence[Mapping[str, Any]], *, iterations: int = 2000, seed: int = 20260920) -> dict[str, Any]:
    """All recorded runs are eligible, independently of score availability."""
    grouped: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in rows:
        if row["group"] in grouped[row["case_id"]]:
            raise ValueError("duplicate trial")
        grouped[row["case_id"]][row["group"]] = row
    result = {}
    for metric in ("reads", "cost_usd", "latency_s"):
        diffs = []
        families: dict[str, list[float]] = defaultdict(list)
        for cid, pair in sorted(grouped.items()):
            if not all(g in pair and isinstance(pair[g].get(metric), (int, float)) and math.isfinite(pair[g][metric]) for g in ("A", "B")):
                continue
            delta = pair["B"][metric] - pair["A"][metric]
            diffs.append({"case_id": cid, "family": pair["A"]["family"], "difference": delta})
            families[pair["A"]["family"]].append(delta)
        keys = sorted(families)
        interval = None
        if len(keys) >= MIN_FAMILIES_FOR_INTERVAL:
            rng = random.Random(seed)
            samples = []
            for _ in range(iterations):
                chosen = [d for _ in keys for d in families[rng.choice(keys)]]
                samples.append(sum(chosen) / len(chosen))
            samples.sort()
            bounds = [samples[int(.025 * iterations)], samples[int(.975 * iterations) - 1]]
            if bounds[0] != bounds[1]:
                interval = bounds
        result[metric] = {"n": len(diffs), "families": len(keys), "mean_difference": mean([d["difference"] for d in diffs]), "interval": interval, "note": "exploratory cluster bootstrap; null interval means descriptive only", "per_case": diffs}
    return result
