# Long Haul

Long Haul is a hardware-agnostic runtime and research framework for coordinating a small heterogeneous inference crew across mismatched compute.

Its central engineering claim is that orchestration should respect **topology, measured capability, explicit authority, independent judgment, and durable provenance** instead of treating every accelerator as interchangeable memory or every model as an anonymous worker.

## What belongs here

The public repository owns provider-neutral runtime concepts and behavior:

- normalized vessels, resources, links, and benchmark evidence;
- persistent crew identities separated from model embodiments;
- execution planning across local, pooled, split, and composed work;
- typed decision and dissent protocols;
- work contracts with externally checked success predicates;
- append-only event provenance, checkpoints, and replayable evidence;
- fixture-backed hardware discovery and runtime adapters.

Concrete operator machines, private networking, privileged identities, and deployment state belong in the separate private Fleet control plane.

## Start here

- [Concepts and invariants](Concepts-and-Invariants.md)
- [Architecture](Architecture.md)
- [Scheduling and execution](Scheduling-and-Execution.md)
- [Work contracts and evidence](Work-Contracts-and-Evidence.md)
- [Hardware and qualification](Hardware-and-Qualification.md)
- [Experiments and evaluation](Experiments-and-Evaluation.md)
- [Fleet boundary](Fleet-Boundary.md)
- [Development](Development.md)

The authoritative architecture is [docs/architecture.md](https://github.com/pH34r-pH/long-haul/blob/main/docs/architecture.md). The wiki is the durable map across those sources, not a replacement for executable contracts.

## Research posture

Long Haul is designed around falsifiable comparisons. The initial crew experiment compares structured independent specialist judgments and dissent-preserving resolution against deterministic, single-planner, and master/subagent baselines under matched task and compute constraints.

A passing test or successful plan is evidence for the specific encoded predicate. It is not a blanket claim that one orchestration philosophy is universally better.
