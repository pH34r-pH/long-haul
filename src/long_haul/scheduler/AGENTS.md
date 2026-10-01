# Scheduler map

[`core.py`](core.py) generates and evaluates plans over normalized vessels, links, profiles, current runtime identities, validations, and benchmark observations. [`../runtime.py`](../runtime.py) supplies validation identity; [`../benchmarks/store.py`](../benchmarks/store.py) supplies workload/evidence models and freshness; [`../benchmark_scheduler.py`](../benchmark_scheduler.py) and [`../benchmark_matrix.py`](../benchmark_matrix.py) collect or replay benchmark inputs. The scheduler ranks only evidence that is compatible with the complete current identity.

## Invariants

- Capacities are finite, typed, unit-compatible, and non-additive across unlike resources.
- A current runtime identity, exact artifact/profile/placement/options, and a valid validation are required for feasibility.
- Performance reuse requires an exact workload, timezone-aware cutoff, measured provenance, finite metrics, and compatible runtime/placement.
- Identical observations may collapse deterministically; distinct observations retain uncertainty until an explicit aggregation policy exists.
- Failed or legacy evidence remains in history but cannot certify or improve ranking.

## Focused validation

```sh
python -m pytest -q tests/test_scheduler_evidence.py tests/test_benchmark_scheduler.py tests/test_anchorage_benchmark_harness.py tests/test_llama_bench_import.py tests/test_admission_regressions.py
```
