import pytest
from pydantic import ValidationError

from long_haul.work import (
    AcceptancePredicate,
    EvidenceState,
    FailureKind,
    PredicateEvidence,
    PredicateKind,
    WorkBudget,
    WorkContract,
    WorkDisposition,
    evaluate_work,
)


def contract(**overrides):
    values = {
        "contract_id": "wc-1", "mission_id": "mission-1", "objective": "Repair telemetry initialization",
        "execution_plan_id": "local:kestrel", "allowed_paths": ["src/telemetry", "tests/telemetry"],
        "forbidden_paths": ["src/telemetry/secrets"], "capabilities": ["read", "edit", "test"],
        "authority_scopes": ["repository:bounded-write"], "budget": WorkBudget(wall_seconds=600, max_attempts=2),
        "success_predicates": [AcceptancePredicate(predicate_id="tests", kind=PredicateKind.TEST, description="targeted tests pass", command=["pytest", "tests/telemetry"])],
    }
    values.update(overrides)
    return WorkContract(**values)


def test_contract_scope_is_bounded():
    item = contract()
    assert item.permits_path("src/telemetry/init.py")
    assert not item.permits_path("src/telemetry/secrets/token.txt")
    assert not item.permits_path("README.md")


@pytest.mark.parametrize("path", ["", "/etc/passwd", "../escape", "src/../escape"])
def test_invalid_scope_rejected(path):
    with pytest.raises(ValidationError):
        contract(allowed_paths=[path])


def test_budget_must_be_positive():
    with pytest.raises(ValidationError):
        WorkBudget(wall_seconds=0)


def test_external_evidence_accepts_contract():
    item = contract()
    result = evaluate_work(item, [PredicateEvidence(predicate_id="tests", state=EvidenceState.PASS, source="pytest exit 0")])
    assert result.disposition is WorkDisposition.ACCEPTED


def test_worker_claim_cannot_satisfy_missing_predicate():
    item = contract(context='worker said "done"')
    result = evaluate_work(item, [])
    assert result.disposition is WorkDisposition.INCOMPLETE


def test_failure_predicate_rejects_even_with_passing_checks():
    item = contract()
    evidence = [PredicateEvidence(predicate_id="tests", state=EvidenceState.PASS, source="pytest exit 0")]
    result = evaluate_work(item, evidence, failure=FailureKind.SCOPE_VIOLATION)
    assert result.disposition is WorkDisposition.REJECTED
