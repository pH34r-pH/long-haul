# Long Haul MVP Architecture

Long Haul separates institutional identity from hardware and model embodiment.

## Core entities

- **Crew member**: persistent operational identity, role, authority scope, competence history, relationships, and continuity record.
- **Embodiment**: current model/runtime/vessel/resource combination implementing a crew member.
- **Vessel**: physical device or host such as Anchorage or Kestrel.
- **Resource**: CPU, GPU, NPU, memory, or sensor within a vessel.
- **Link**: measured relationship between resources or vessels (PCIe, memory, Tailscale, Ethernet, etc.).
- **Mission**: unit of work with objectives, constraints, and outcome metrics.
- **Decision**: structured proposal with independently captured positions and an explicit resolution mode.
- **Execution plan**: mapping from a mission to crew, vessels, resources, runtimes, and execution mode.

## Hard invariants

1. Crew identity is not a model checkpoint or process ID.
2. Aggregate memory is never treated as uniform memory without topology evidence.
3. Model size does not confer rank or authority.
4. Consequential decisions preserve independent initial judgments before deliberation when feasible.
5. Dissent is a protocol state, not a logging accident.
6. Permissions and authority are explicit and externally enforced.
7. Higher-priority safety/cooperation constraints cannot be compensated away by throughput gains.
8. Cross-node PIPELINE execution is eligible only when the measured network path meets policy requirements.
9. Tailscale is the authenticated transport plane; application protocols remain ordinary network services.
10. Every material decision and embodiment transition is provenance-bearing and replayable.

## Initial fleet topology

### Anchorage

Fixed station. `ANC-G0` and `ANC-G1` are both GTX 1070s but are explicitly modeled as asymmetric because the B650 Tomahawk motherboard gives the secondary slot a lower-bandwidth host path. Benchmarks, not nominal GPU equality, determine split ratios.

### Kestrel

Jetson Orin Nano ship. Primarily an independent small-model/edge worker, with optional participation in cross-node execution. It should degrade gracefully when disconnected from Anchorage.

## Initial crew

### NAV-01 / Mako

Navigator. Proposes execution plans and routes work.

### ENG-01 / Patch

Engineer. Evaluates hardware/runtime feasibility, memory fit, and topology constraints.

The MVP experiment compares this two-role consent/dissent architecture against simpler baselines.

## Execution modes

- `LOCAL`: one vessel, one or more local resources.
- `POOL`: independent work distributed among vessels.
- `PIPELINE`: one model split across resources/nodes when necessary for capability.
- `COMPOSE`: specialist crew/models cooperate on one task.

## Control flow

1. Discover/refresh fleet telemetry.
2. Build candidate execution plans.
3. NAV-01 independently ranks candidates.
4. ENG-01 independently checks constraints and ranks/objects.
5. Store both initial positions before cross-exposure.
6. Apply the decision protocol: consent, stand-aside, objection integration, bounded experiment, scoped authority, escalation, or recorded disagreement.
7. Execute an eligible plan.
8. Record outcome, performance, calibration, dissent, and repair data.
9. Update competence/trust statistics from observed results rather than rhetoric.

## Persistence

The MVP should use append-only event records plus materialized YAML/JSON views. SQLite is acceptable initially for indexes and query convenience; event provenance should remain exportable and human-readable.
