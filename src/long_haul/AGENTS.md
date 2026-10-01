# Runtime map

`src/long_haul/` is the portable implementation layer. [`models.py`](models.py) is the shared domain boundary: registry/discovery produce typed vessels and crew; runtime/adapters validate and execute inference profiles; scheduler/benchmarks select eligible plans; decisions, events, and work record the resulting protocol state.

## Local routing

- [`models.py`](models.py), [`runtime.py`](runtime.py), [`registry.py`](registry.py): canonical domain, runtime identity, and manifest loading.
- [`discovery/`](discovery/): edge probes and parsers; unknown capability must remain unknown.
- [`scheduler/`](scheduler/) and [`benchmarks/`](benchmarks/): typed admission and compatible measured-evidence reuse.
- [`work/`](work/): contract-scoped execution/acceptance; see its [map](work/AGENTS.md).
- [`events/`](events/) and [`decisions/`](decisions/): append-only provenance and dissent-preserving resolution.
- [`adapters/`](adapters/), [`mcp/`](mcp/), [`reference.py`](reference.py), [`experiments/`](experiments/): integration edges over the core contracts.

Do not introduce provider-specific branching into `models.py` or scheduler policy. Extend an adapter/protocol and bind it to a typed profile instead.

## Focused validation

```sh
python -m ruff check src tests
python -m pytest -q tests/test_models.py tests/test_core.py tests/test_runtime.py tests/test_reference.py
```

For scheduler, discovery, or work changes, follow the more specific nested map and command.
