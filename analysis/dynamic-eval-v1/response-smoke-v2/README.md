# Response grader 1.1 smoke verification

The top-level `report.json` and `calls.jsonl` preserve the first sandbox attempt, which failed to connect to the API. Its 1/12 match is entirely an infrastructure artifact, not a semantic judge measurement.

The successful authorized-network run is in `retry-network-1/`:

- DeepSeek returned 11 actual model responses for 12 disclosed synthetic cases; the deliberately missing-observation case abstained without a model call.
- All 12 results matched the developer-written expectations: 6 pass, 5 fail, 1 missing-observation unknown.
- Semantic unknown: 0. Protocol unknown: 0. Length finishes: 0.
- This checks the grader harness and known counterexamples; it is not independent human calibration.

The manifest records grader source and synthetic-case hashes; calls.jsonl preserves request/response exchanges. Existing pilot artifacts and evaluation results were not changed.
