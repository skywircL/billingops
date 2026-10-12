# Same-artifact response regrade with grader 1.1

Use `retry-network-1/` for the successful DeepSeek verification. The top-level files preserve an earlier sandbox APIConnectionError attempt and are not evidence of semantic grader quality.

| Original pilot artifact | Required assertions | Pass | Fail | Semantic unknown | Protocol unknown | Missing-observation unknown |
|---|---:|---:|---:|---:|---:|---:|
| G06-t0 | 4 | 2 | 1 | 0 | 0 | 1 |
| G27-t0 | 5 | 5 | 0 | 0 | 0 | 0 |

Each artifact still encountered one `length` response at the 4096-token limit; in both cases the second allowed attempt returned a verdict. The change resolved the tested protocol abstentions through a larger ceiling and successful retry; it does not guarantee that truncation cannot recur.

G06's failure concerns the response omitting the concrete unresolved refund state while only stating the approval policy. Its absent execution observation remains `unknown`, rather than being filled from fixture expectations. G27's five required assertions passed, including the captured progress response. Privacy and tool-result-hallucination semantic checks passed for both; these remain provisional model judgments.

The two source artifact SHA256 hashes are recorded in per-case manifests and reports, and post-run checks confirm the originals are unchanged. All calls, raw verdicts, usage and per-attempt finish reasons are retained. This is offline grading of the same Agent trajectories, not a new Agent trial or independent human calibration. No original pilot result was overwritten.
