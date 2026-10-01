# Long Haul repository map

This is the implementation-oriented map for the public Long Haul repository. It complements the normative [architecture](architecture.md), the explicit design-only [north star](north-star.md), and the public/private [repository boundary](repository-boundary.md). The wiki under `docs/wiki/` is generated navigation, not a second implementation source.

## Architecture from the root downward

```text
contracts/ + crew/ + fixture manifests
        │
        ▼
discovery/registry ──► models.py (typed vessels, resources, links, crew, plans)
        │                         │
        │                         ├── runtime.py + adapters/ (identity, validation, execution)
        │                         ├── scheduler/ + benchmarks/ (admission and measured reuse)
        │                         ├── decisions/ (positions, authority, dissent)
        │                         ├── events/ (append-only history and materialization)
        │                         └── work/ (contracts, checkpoints, progress, acceptance)
        │
        └── experiments/ + mcp/ + reference.py are edges over those contracts
```

The normal data flow is discovery or a checked-in fixture → normalized domain models → runtime/profile validation → scheduler candidates → decision/work execution → events and independent evidence. `src/long_haul/models.py` is the shared type boundary; adapters and probes must not smuggle provider-specific assumptions into the scheduler.

## Important files and relationships

| Area | Source of truth | Reads/writes | Change route |
| --- | --- | --- | --- |
| Domain identity and topology | [`src/long_haul/models.py`](../src/long_haul/models.py) | Registry, discovery, scheduler, events, tests | Model/invariant change; run model and scheduler tests |
| Manifests and probes | [`src/long_haul/registry.py`](../src/long_haul/registry.py), [`src/long_haul/discovery/`](../src/long_haul/discovery/) | YAML/JSON/CUE inputs become `Vessel`/`CrewMember` | Preserve unknown capability and measured-vs-simulated provenance |
| Runtime identity | [`src/long_haul/runtime.py`](../src/long_haul/runtime.py), [`src/long_haul/adapters/`](../src/long_haul/adapters/) | Profiles, binaries, validation, execution results | Keep runtime identity separate from crew/artifact identity |
| Planning and evidence | [`src/long_haul/scheduler/core.py`](../src/long_haul/scheduler/core.py), [`src/long_haul/benchmarks/`](../src/long_haul/benchmarks/) | Validations and measured observations produce eligible/ranked candidates | Require exact profile, placement, runtime, workload, and compatible evidence |
| Durable state | [`src/long_haul/events/store.py`](../src/long_haul/events/store.py) | Append-only events materialize fleet state | Never rewrite historical events or turn a projection into authority |
| Work and acceptance | [`src/long_haul/work/`](../src/long_haul/work/) | Contracts produce bounded attempts, checkpoints, progress telemetry, and evidence packets | External predicates decide completion; worker self-report does not |
| Decisions | [`src/long_haul/decisions/engine.py`](../src/long_haul/decisions/engine.py) | Independent positions become a recorded resolution | Preserve dissent and enforce scoped authority |
| Public contracts | [`contracts/`](../contracts/), [`crew/`](../crew/) | Requirements tooling and registry fixtures | Keep machine-readable contracts durable and fixtures labelled |
| Public infrastructure | [`infra/azure/`](../infra/azure/) | Reference-only deployment/IaC | Do not put private Fleet state or credentials here |

## Invariants that constrain changes

- Crew identity, embodiment, model artifact, runtime identity, and session identity remain distinct.
- Resource capacities are typed and finite; unlike resources and non-additive capacities are never silently summed.
- `PIPELINE` requires measured link/topology eligibility; nominal aggregate memory is not evidence.
- Independent positions are captured before deliberation where the protocol permits, and dissent is retained as data.
- The append-only event history is authoritative; checkpoints, digests, and other materializations are rebuildable.
- Acceptance is bound to an exact candidate fingerprint and external work-contract predicates.
- Historical measurements, failed evidence, and dissenting/scientific records remain readable; later policy must not rewrite them.
- Simulated fixtures and measured hardware evidence remain visibly distinct.

## Change routing and focused validation

Use the narrowest command that covers the changed boundary, then run the full repository path before a code PR.

| Change | Focused command |
| --- | --- |
| Models, topology, decisions | `python -m pytest -q tests/test_models.py tests/test_core.py tests/test_decisions.py tests/test_admission_regressions.py` |
| Scheduler/runtime/benchmark evidence | `python -m pytest -q tests/test_scheduler_evidence.py tests/test_benchmark_scheduler.py tests/test_anchorage_benchmark_harness.py tests/test_llama_preflight.py tests/test_llama_bench_import.py` |
| Work contracts, evaluator, checkpoints, progress, Pi adapter | `python -m pytest -q tests/test_work_contracts.py tests/test_evaluator.py tests/test_checkpoints.py tests/test_progress.py tests/test_pi_rpc.py` |
| Discovery/parsers | `python -m pytest -q tests/test_parsers.py tests/test_windows_discovery.py tests/test_reference.py` |
| Contracts/requirements | `python -m unittest tools.test_requirements tools.test_requirements_boundaries` |
| Packaging | `python -m pytest -q tests/test_offline_package.py && python scripts/build_offline_package.py --output-dir /tmp/long-haul-offline-package` |
| Any Python source | `python -m ruff check src tests` |
| Documentation-only edits | `git diff --check`; verify every relative link in the changed Markdown resolves from its file directory |

The current CI-equivalent source checks are in [`ci.yml`](../.github/workflows/ci.yml), the requirements workflow, and the audit workflow. The wiki is synchronized by [`wiki-sync.yml`](../.github/workflows/wiki-sync.yml) only from `main`.

## Smallest future CI integration

This wave intentionally adds no new guard and changes no workflow behavior. Existing CI already runs on pull requests, and the structural audit is pull-request-wide. After the Fleet/DSL/Portfolio coherence review, the smallest useful integration is one shared relative-link/reference check added to that existing audit job, with no parallel workflow and no duplicate repository-specific guard.
