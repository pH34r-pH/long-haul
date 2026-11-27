# Replay a retained repository trajectory

Use `replay-trajectory` with a retained canonical `HarnessResult` and its semantic JSONL. Obtain the result fingerprint from the independent record of the attempt and use current resource-policy admission for access. The replay applies a caller-supplied read budget to the result, trace, and optional progress policy.

```sh
python -m long_haul replay-trajectory result.json semantic-trace.jsonl \
  --expected-result-fingerprint "$RETAINED_RESULT_FINGERPRINT" \
  --max-bytes "$READ_BUDGET_BYTES"
```

Add `--progress-policy progress-policy.json` to replay the same trajectory with alternate thresholds. For example, `{"repeated_failure_limit": 3}` changes the failure threshold and retains the other defaults. The receipt records the effective policy and the observed replay source and Python dependency identities. It rejects completed actions with mixed normalization identities because their argument fingerprints may be incomparable.

The receipt preserves the original terminal state, candidate disposition, and acceptance state. It separates lifecycle events from completed tool actions and identifies the sequences where the replay would signal an intervention. Replay reads the entire observed trace, including events after the first hypothetical stop. Zero-event failures remain visible. The summed durations are observed tool timings, with missing durations counted separately. Critical-path costs, dependency overlap, wall time saved, evaluator acceptance, and model performance require separate evidence.

The fixture exercises the repository trace inputs for Long-Haul #118 and the #64 benchmark. Scored qualification requires the frozen task, independent evaluator, and admitted execution specified by those issues. [Trace-driven research](https://github.com/pH34r-pH/long-haul/issues/118) · [Repository benchmark](https://github.com/pH34r-pH/long-haul/issues/64)
