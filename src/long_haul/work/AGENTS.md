# Work and acceptance map

The work boundary turns a bounded objective into externally checkable evidence. [`contracts.py`](contracts.py) defines scope, budgets, capabilities, and success/failure predicates; [`checkpoints.py`](checkpoints.py) reduces authoritative events into rebuildable resumable state; [`progress.py`](progress.py) classifies steps and bounded recovery; [`evaluator.py`](evaluator.py) evaluates a candidate through a read-only target and emits predicate evidence. [`../adapters/pi_rpc.py`](../adapters/pi_rpc.py) is only an execution adapter.

## Invariants

- A model or harness saying “done” is not acceptance evidence.
- Evidence is bound to the contract and exact candidate fingerprint; a changed candidate invalidates prior certification.
- Duplicate predicate observations are rejected within an attempt; historical attempts remain separate.
- Checkpoints/digests are disposable projections of the event log, not new authority or unbounded conversation memory.
- Progress interventions record bounded provenance, not prompt/tool payloads.

## Focused validation

```sh
python -m pytest -q tests/test_work_contracts.py tests/test_evaluator.py tests/test_checkpoints.py tests/test_progress.py tests/test_pi_rpc.py
```
