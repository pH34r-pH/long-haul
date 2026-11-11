# Work and acceptance map

The work boundary turns a bounded objective into externally checkable evidence. [`contracts.py`](contracts.py) defines scope, budgets, capabilities, and success/failure predicates; [`checkpoints.py`](checkpoints.py) reduces authoritative events into rebuildable resumable state; [`progress.py`](progress.py) classifies steps and bounded recovery; [`evaluator.py`](evaluator.py) evaluates a candidate through a read-only target and emits predicate evidence. [`../adapters/pi_rpc.py`](../adapters/pi_rpc.py) is only an execution adapter.

[`repository_models.py`](repository_models.py) owns optional repository/context identities; [`repository_git.py`](repository_git.py) reads frozen objects; [`repository_context.py`](repository_context.py) applies caller-admitted policy, selects bounded evidence, and shares the worker projection. Registry admission is authenticated caller input, never repository text. Run `tests/test_repository_context.py` with the work tests for this boundary.

[`repository_evaluation_models.py`](repository_evaluation_models.py), [`repository_candidate.py`](repository_candidate.py), [`repository_evaluator.py`](repository_evaluator.py), and [`repository_sandbox.py`](repository_sandbox.py) compose exact-candidate evaluation and maintained isolation. Run `tests/test_repository_evaluator.py` for hermetic semantics; `tests/test_repository_gvisor.py` qualifies real isolation only under the strict hosted job. Sealed profiles and expected outputs remain control-plane input and must not enter worker payloads.

The Pi worker checks current READ and AI_AUGMENT policy for all existing files before projecting any bytes. After stopping the worker, changed candidate bytes require a completed, still-authorized derivation. These checks do not by themselves qualify the full adversarial security profile.

Native write completions must match their authorized UTF-8 payload. Edits are limited to unique exact LF-only UTF-8 replacements; a changed target must match the resulting bytes before a derivation can be recorded.

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
