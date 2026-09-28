# Work contracts and evidence

A WorkContract is Long Haul's boundary between “a model was asked to do something” and “an engineering task has objective acceptance semantics.”

## A contract owns

- objective and repository/scope boundary;
- permitted capabilities and authority;
- resource/time budgets;
- external success and failure predicates;
- provenance and execution-plan linkage;
- final disposition.

A coding harness may translate the contract into model-specific guidance, recover from transient provider errors, and provide tool access. It does not get to redefine success.

## Independent acceptance

Implementation-time checks are worker feedback. Acceptance evidence is produced by an evaluator that receives the candidate through a read-only interface and evaluates the contract predicates.

Evidence is bound to:

- the exact candidate fingerprint;
- evaluator/runtime identity;
- the predicate set and results.

Changing the candidate invalidates evidence from the older fingerprint. Later regressions create new evidence instead of rewriting history.

## Durable context

The append-only event log is authoritative. A WorkCheckpoint is a compact deterministic materialization of contract-scoped facts, constraints, verified artifacts, subgoal state, and unresolved failures. A TaskDigest renders a bounded subset for a new model/harness session and explicitly states omissions.

A chat transcript is not durable institutional memory.

See the architecture document and `src/long_haul/work/` for the executable contract.
