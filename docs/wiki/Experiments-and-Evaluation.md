# Experiments and evaluation

The initial Long Haul research question is deliberately narrow: does a structured, competence-scoped, dissent-preserving two-member crew add measurable value over simpler orchestration under matched constraints?

## Baselines

The experiment plan includes:

1. deterministic benchmark-based scheduling;
2. a single LLM planner;
3. a master/subagent planner-advisor hierarchy;
4. Long Haul's NAV-01 + ENG-01 protocol with independent assessment and typed dissent.

## Task variation

Controlled tasks vary model/artifact size, quantization, context, latency versus throughput goals, available memory, asymmetric topology, edge-vessel availability, link quality, runtime availability, and stale or misleading telemetry.

## Measurements

Evaluation includes feasibility/failure, task success, execution latency/throughput, decision latency, coordination cost, energy where practical, regret relative to measured alternatives, calibration, disagreement, false consensus, usefulness of objections, and recurrence after repair.

## Anti-sycophancy control

Consequential runs preserve independent judgments before peer positions are revealed. Post-discussion agreement is therefore not automatically treated as evidence of correctness.

## Expansion rule

The project should not add crew complexity merely because more agents are available. Expansion is justified by measured value relative to coordination cost.

See [docs/experiments.md](https://github.com/pH34r-pH/long-haul/blob/main/docs/experiments.md).
