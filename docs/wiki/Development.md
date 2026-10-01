# Development

## Repository map

The source-grounded [repository map](../repository-map.md) and scoped `AGENTS.md` files are the detailed contributor map. They record the downward architecture, data flow, invariants, change routing, and exact focused commands; this wiki page remains the short navigation layer.

- `src/long_haul/` — runtime, topology, scheduling, decisions, events, work/evaluation, adapters, and benchmarks.
- `tests/` — unit and contract coverage.
- `contracts/` — durable machine-readable contracts.
- `crew/` — public crew definitions/fixtures.
- `docs/` — architecture, experiments, requirements, and hardware-validation guidance.
- `infra/azure/` — public reference infrastructure, distinct from private Fleet state.
- `scripts/` and `tools/` — packaging and developer/validation helpers.

## Environment

The repository carries Python packaging/lock data and a reproducible development-environment definition. Follow the current `pyproject.toml`, `uv.lock`, and `devenv.*` files rather than copying version assumptions from old issues.

## Contribution rules

Changes that touch scheduling or execution must preserve typed resource/topology semantics. Changes to acceptance must preserve the independent-evaluator boundary. New hardware claims require measurement evidence. New model/runtime support should extend adapters and profiles rather than special-case crew identity.

Run the full repository test/validation path before opening a pull request. See [CONTRIBUTING.md](https://github.com/pH34r-pH/long-haul/blob/main/CONTRIBUTING.md).

For a narrow change, begin with the relevant command from the repository map, for example `python -m pytest -q tests/test_scheduler_evidence.py tests/test_benchmark_scheduler.py` for scheduler/evidence work or `python -m unittest tools.test_requirements tools.test_requirements_boundaries` for requirements contracts. Pull requests also run the changed-file documentation/artifact guard in the structural audit; it checks only living Markdown and preserves explicit historical/evidence roots.
