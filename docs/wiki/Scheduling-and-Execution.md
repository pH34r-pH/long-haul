# Scheduling and execution

Long Haul schedules over **validated inference profiles on measured topology**, not a fictional pool of aggregate RAM/VRAM.

## Execution modes

- **LOCAL** — one vessel executes the work locally.
- **POOL** — independent jobs are distributed among vessels.
- **PIPELINE** — one model or workflow spans resources when capability requires it.
- **COMPOSE** — specialist crew/models cooperate on one task.

These modes describe orchestration. Resident, offloaded, split, streaming, or parallel runtime strategies belong to inference profiles.

## Plan construction

A candidate plan combines a mission, crew assignment, runtime, artifact, resources, and mode. Eligibility depends on observed capability: runtime support, capacity, current load, topology, link measurements, and policy.

Cross-vessel pipeline execution requires measured network evidence. Losing a direct path should degrade eligibility rather than silently pretending the previous topology still exists.

## Independent review

The initial two-role design uses NAV-01 for routing/planning and ENG-01 for hardware/runtime feasibility. Their initial judgments are persisted before deliberation when practical so later agreement can be distinguished from genuine independent agreement.

## Progress and recovery

Harness actions are classified independently of model prose: information acquisition, state change, verification, recovery, or no-progress. Deterministic signatures make repeated calls/failures visible. Verified progress resets the no-progress budget; repeated non-progress can trigger an explicit intervention, checkpoint, retry, or escalation.

The scheduler and progress machinery live under `src/long_haul/scheduler/` and `src/long_haul/work/`.
