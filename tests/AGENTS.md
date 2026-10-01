# Test evidence map

Tests are grouped by the contract they observe, not by implementation convenience. Domain/model tests cover [`src/long_haul/models.py`](../src/long_haul/models.py); scheduler evidence tests cover identity and reuse; work tests cover external acceptance and recovery; fixture tests cover discovery and reference topology; packaging tests cover exact offline bundles.

Keep scientific, dissenting, failed, and historical fixtures intact. Add a new fixture or assertion when behavior changes; do not rewrite a record merely to make a new implementation pass.

## Focused commands

```sh
python -m pytest -q tests/test_models.py tests/test_core.py tests/test_decisions.py
python -m pytest -q tests/test_scheduler_evidence.py tests/test_benchmark_scheduler.py tests/test_anchorage_benchmark_harness.py
python -m pytest -q tests/test_work_contracts.py tests/test_evaluator.py tests/test_checkpoints.py tests/test_progress.py
python -m pytest -q
```
