# MVP Experiment Plan

The first Long Haul experiment should test whether a structured two-member crew adds measurable value over simpler orchestration.

## Baselines

1. Deterministic scheduler using benchmark lookup only.
2. Single LLM planner.
3. Master/subagent hierarchy with one planner and one advisor.
4. Long Haul `NAV-01` + `ENG-01` with independent assessment, typed dissent, consent/scoped authority, and preserved provenance.

## Task family

Use repeated execution-plan selection tasks over controlled variations of:

- model size and quantization
- context length
- latency-sensitive vs throughput-sensitive objectives
- available VRAM/RAM
- asymmetric accelerator topology
- edge vessel available/unavailable
- Tailscale direct vs poor/relayed path
- runtime availability
- injected stale or misleading telemetry

## Metrics

At minimum collect:

- plan feasibility / failure rate
- task success
- time to first token and decode throughput after plan execution
- decision latency
- coordination-token cost
- energy where practical
- plan regret relative to best measured plan
- calibration of confidence
- disagreement rate
- false-consensus rate
- objection usefulness
- rupture recurrence after repair

## Net synergy

For task distribution D, define a mission-specific value function V and compare the cooperative system to the best individual baseline:

`S_net(A,B) = E_D[V(A⊕B) - max(V(A), V(B))] - C_coordination`

The exact weighting of latency, success, energy, and token cost must be reported rather than hidden.

## Anti-sycophancy control

Consequential runs store each participant's initial judgment before revealing peer positions. Analyze both independent agreement and post-deliberation agreement. Universal post-discussion agreement is not treated as evidence of correctness by itself.

## Stop condition

Do not expand the crew until the two-role architecture either demonstrates positive net value or produces a specific falsifiable question that requires additional roles.

## Hermetic repository benchmark

The [repository attempt fixture](../tests/test_repository_attempt.py) selects context from a frozen toy Git task, runs the scripted harness, freezes the stopped worker's bytes, and records the independent evaluator packet in the existing event store. A corrected candidate passes both checks. A second candidate returns the public answer and fails the sealed case. Both report a successful edit and a settled harness. The synthetic evaluator checks a fixed set of candidate bytes and inputs without executing worker code.

Use `verify_semantic_trace` to read back the canonical result and trace against an independently retained result pin. Then call `long_haul.work.repository_attempt.readback_repository_attempt` with the frozen contract and evaluator profile, the read-back result, and the independently pinned evaluator packet. It checks that the records describe the same attempt and frozen candidate before recomputing the evaluator's disposition.

```sh
python -m pytest -q tests/test_repository_attempt.py
```

This fixture covers the Linux hermetic evidence flow for P0. Runtime isolation, real Pi execution, and scored comparisons have separate qualification gates. [Repository benchmark](https://github.com/pH34r-pH/long-haul/issues/64)
