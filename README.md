# Long Haul

Long Haul is a heterogeneous local inference crew: a topology-aware system for coordinating models, compute resources, and persistent crew identities across mismatched hardware over Tailscale.

The project is intentionally built for the long haul: hardware, models, runtimes, and even assumptions about artificial identity may change over years or decades. The architecture therefore separates **crew identity**, **model embodiment**, **vessels**, **compute resources**, and **missions**.

## MVP thesis

The first technical hypothesis is narrow and falsifiable:

> A structurally respectful, dissent-preserving, competence-scoped two-agent scheduler can outperform a conventional single-agent or master/subagent scheduler under identical compute constraints.

The initial crew is:

- `NAV-01` — navigator / routing planner
- `ENG-01` — hardware/runtime specialist

The initial fleet is:

- `Anchorage` — fixed desktop station with asymmetric dual GTX 1070 topology
- `Kestrel` — NVIDIA Jetson Orin Nano ship, connected over Tailscale

## Execution modes

- **LOCAL** — one vessel executes locally
- **POOL** — independent jobs across vessels
- **PIPELINE** — one model split across resources/nodes when capacity requires it
- **COMPOSE** — specialist crew members/models cooperate on a task

## Design invariants

Long Haul does not treat aggregate VRAM/RAM as a uniform pool. It models topology, measured bandwidth, latency, runtime support, and current load. It does not assume larger models outrank smaller specialists. It preserves independent judgments, dissent, provenance, calibration, and repair history.

See `docs/architecture.md`, `docs/articles.md`, and `docs/experiments.md`.

## Status

MVP scaffold. No production scheduler yet.
# Long Haul

Long Haul is a hardware-agnostic core for a small, heterogeneous local inference
crew. The shared schema, provenance/event log, consent protocol, fixture-backed
topology adapters, benchmark store, and scheduler are implementable without
physical fleet access. See [architecture](docs/architecture.md) and the
[hardware-validation runbook](docs/hardware-validation.md).
