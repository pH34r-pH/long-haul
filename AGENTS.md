# Long Haul agent map

Read [`docs/repository-map.md`](docs/repository-map.md) for the source-grounded architecture, invariants, change routing, and focused validation commands. Read [`docs/architecture.md`](docs/architecture.md) before changing runtime semantics and [`CONTRIBUTING.md`](CONTRIBUTING.md) before preparing a PR.

## Repository boundaries

- `src/long_haul/` owns portable runtime behavior and domain contracts.
- `contracts/` and `crew/` are durable public machine-readable inputs.
- `tests/` is executable evidence for the public behavior and invariants.
- `docs/` contains authoritative prose plus the generated wiki source in `docs/wiki/`.
- `infra/azure/` is reference infrastructure only; private Fleet state is out of scope.
- `scripts/` and `tools/` package and validate the public source.

## Working rules

- Preserve the separation between crew identity, embodiment, artifact, runtime, vessel, and session.
- Preserve typed topology and measured-evidence semantics; do not infer capability from nominal size or model self-report.
- Treat events and scientific/qualification records as immutable history. Add a correction or superseding record rather than rewriting an unsuccessful, dissenting, or historical result.
- Keep design-only north-star claims labelled as design; do not document an unimplemented component as current behavior.
- Route changes to the narrowest scoped map below and run its focused command before broader validation.

## Scoped maps

- [`src/long_haul/AGENTS.md`](src/long_haul/AGENTS.md) — runtime data flow and module ownership.
- [`src/long_haul/work/AGENTS.md`](src/long_haul/work/AGENTS.md) — contracts, checkpoints, progress, and acceptance.
- [`src/long_haul/scheduler/AGENTS.md`](src/long_haul/scheduler/AGENTS.md) — plan admission and evidence reuse.
- [`src/long_haul/discovery/AGENTS.md`](src/long_haul/discovery/AGENTS.md) — probes and normalized topology.
- [`tests/AGENTS.md`](tests/AGENTS.md) — executable evidence routing.
- [`contracts/AGENTS.md`](contracts/AGENTS.md) — durable requirements and vessel schemas.
- [`docs/AGENTS.md`](docs/AGENTS.md) — documentation authority and historical records.
- [`infra/azure/AGENTS.md`](infra/azure/AGENTS.md) — public reference infrastructure boundary.
