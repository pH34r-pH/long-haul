# Architecture

Long Haul has three broad layers: **observation**, **decision**, and **execution/evidence**.

```text
discovery + benchmarks
        |
        v
normalized topology
        |
        v
candidate execution plans
        |
        +--> independent crew positions
        |            |
        |            v
        +------ decision protocol
                     |
                     v
               eligible plan
                     |
                     v
                 execution
                     |
                     v
        outcome + evidence + events
```

## Observation layer

Discovery adapters turn machine-specific information into normalized vessels, resources, links, runtimes, and benchmarks. Fixture data is marked simulated; physical measurements must carry evidence rather than borrowing fixture credibility.

## Decision layer

The scheduler proposes feasible plans from typed resource requirements and topology. Crew members can independently rank, object, consent, stand aside, or request bounded experiments. Initial positions are recorded before cross-exposure for consequential decisions where feasible.

## Execution layer

Execution adapters perform work through ordinary runtime/network interfaces. Tailscale is an authenticated transport plane, not the application protocol itself.

## Evidence layer

Append-only events preserve decisions, attempts, embodiment transitions, verification, and repair history. Materialized checkpoints are disposable views of that history. Independent evaluators bind acceptance evidence to an exact candidate fingerprint.

## Why this separation matters

A scheduler can change without redefining crew identity. A model can change without erasing competence history. A machine can be reimaged without changing the public runtime contract. A worker can claim success without satisfying the external acceptance predicate.

See [docs/architecture.md](https://github.com/pH34r-pH/long-haul/blob/main/docs/architecture.md) for exact model definitions.
