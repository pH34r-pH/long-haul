# Concepts and invariants

Long Haul deliberately separates identities that are often conflated in agent systems.

## Core entities

**Crew member** is a persistent operational identity with role, authority, competence history, relationships, and continuity.

**Embodiment** is the current model/runtime/vessel/resource combination implementing a crew member.

**Vessel** is a physical device or host represented by a normalized manifest.

**Resource** is an individually addressable CPU, GPU, NPU, memory, storage, sensor, or related capability. Unlike capacities are never summed implicitly.

**Link** records a measured relationship between resources or vessels: local interconnect, memory path, Ethernet, Tailscale, or another transport.

**Mission** is a unit of work with objectives and outcome metrics.

**Work contract** is a bounded engineering task with scope, authority, budgets, success/failure predicates, and provenance. It is not a model prompt.

**Execution plan** maps a mission to crew, vessels, resources, runtimes, and an execution mode.

**Model artifact** describes model lineage and packaging. It is not crew identity.

## Hard invariants

The architecture refuses several convenient shortcuts:

- model size does not imply authority;
- nominal or aggregate memory does not prove feasibility;
- consequential decisions preserve independent initial judgments when feasible;
- dissent is explicit protocol state;
- authority is externally enforceable rather than prompt-only;
- topology and measured links govern split execution;
- material decisions and embodiment transitions remain replayable;
- engineering completion requires external predicate evidence rather than worker self-report.

These invariants are the project contract. A performance optimization that violates them changes the experiment rather than merely improving an implementation.

See [docs/architecture.md](https://github.com/pH34r-pH/long-haul/blob/main/docs/architecture.md).
