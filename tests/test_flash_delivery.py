import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from long_haul.work import (
    EvidenceState,
    FlashDeliveryFixture,
    FlashExecutionExtensionV1,
    LeaseError,
    PredicateEvidence,
    VerificationError,
    WorkContract,
    WorkDisposition,
    evaluate_work,
)

FIXTURE = Path(__file__).parent / "fixtures/work/flash-f0-toy.json"


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def toy_contract():
    return WorkContract.model_validate(json.loads(FIXTURE.read_text())["contract"])


def toy_verifier(expected):
    def verify(contract, result):
        matches = (type(result) is dict and set(result) == {"count"}
                   and type(result["count"]) is int and result["count"] == expected["count"])
        evaluation = evaluate_work(contract, [PredicateEvidence(
            predicate_id="count",
            state=EvidenceState.PASS if matches else EvidenceState.FAIL,
            source="independent deterministic toy verifier",
        )])
        return evaluation.disposition is WorkDisposition.ACCEPTED
    return verify


def make_fixture(clock, *, contract=None, expected=None):
    payload = json.loads(FIXTURE.read_text())
    return FlashDeliveryFixture(
        job_id="toy-job-1", contract=contract or WorkContract.model_validate(payload["contract"]), lease_seconds=10,
        clock=clock, id_factory=lambda value: value,
        verifier=toy_verifier(expected or payload["expected_result"]),
    )


def test_toy_fixture_reuses_work_contract_acceptance_and_is_explicitly_synthetic():
    payload = json.loads(FIXTURE.read_text())
    assert payload["fixture_status"] == "synthetic_contract_test_only_not_hardware_qualification"
    contract = WorkContract.model_validate(payload["contract"])
    assert contract.contract_id == "toy-f0-contract-v1"
    assert contract.success_predicates[0].predicate_id == "count"
    assert contract.flash_execution.admits(
        capability_state="available",
        observed_memory_mib=payload["synthetic_admission_envelope"]["observed_memory_mib"],
    )


def test_f0_unknown_missing_memory_and_failed_initialization_decline():
    extension = FlashExecutionExtensionV1()
    assert not extension.admits(capability_state="unknown", observed_memory_mib=64)
    assert not extension.admits(capability_state="available", observed_memory_mib=None)
    assert not extension.admits(capability_state="available", observed_memory_mib=64, initialization_succeeded=False)
    assert not extension.admits(capability_state="unavailable", observed_memory_mib=64)
    with pytest.raises(ValidationError):
        FlashExecutionExtensionV1(tier="F1")


def test_claim_renew_expiry_redelivery_and_stale_fencing():
    clock = FakeClock()
    queue = make_fixture(clock)
    first = queue.claim()
    assert (first.job_id, first.contract_id) == ("toy-job-1", "toy-f0-contract-v1")
    assert first.attempt_id != first.lease_id
    assert queue.claim() is None
    renewed = queue.renew(first.lease_id)
    assert renewed.lease_expires_at == 110.0
    clock.advance(10)
    with pytest.raises(LeaseError, match="expired"):
        queue.renew(first.lease_id)
    with pytest.raises(LeaseError, match="expired"):
        queue.complete(first.lease_id, {"count": 5})
    with pytest.raises(LeaseError, match="expired"):
        queue.abandon(first.lease_id)
    second = queue.claim()
    assert second.attempt_id != first.attempt_id
    assert second.lease_id != first.lease_id
    with pytest.raises(LeaseError, match="stale"):
        queue.renew(first.lease_id)
    with pytest.raises(LeaseError, match="stale"):
        queue.complete(first.lease_id, {"count": 5})
    clock.advance(4)
    renewed_again = queue.renew(second.lease_id)
    assert renewed_again.lease_expires_at == 124.0
    with pytest.raises(LeaseError, match="stale"):
        queue.abandon(first.lease_id)
    clock.advance(10)
    with pytest.raises(LeaseError, match="expired"):
        queue.renew(second.lease_id)
    with pytest.raises(LeaseError, match="expired"):
        queue.complete(second.lease_id, {"count": 5})
    with pytest.raises(LeaseError, match="expired"):
        queue.abandon(second.lease_id)
    third = queue.claim()
    assert third.attempt_id != second.attempt_id


def test_abandon_redelivers_and_completion_is_idempotent():
    queue = make_fixture(FakeClock())
    first = queue.claim()
    queue.abandon(first.lease_id)
    second = queue.claim()
    assert second.attempt_id != first.attempt_id
    expected = {"count": 5}
    wrong = {"count": 4, "accepted": True}
    with pytest.raises(VerificationError, match="rejected"):
        queue.complete(second.lease_id, wrong)
    with pytest.raises(VerificationError, match="finite JSON"):
        queue.complete(second.lease_id, {"count": float("nan")})
    accepted = queue.complete(second.lease_id, expected)
    assert accepted == expected
    expected["count"] = 100
    accepted["count"] = 200
    returned_copy = queue.completed_result
    returned_copy["count"] = 300
    assert queue.completed_result == {"count": 5}
    assert queue.complete(second.lease_id, {"count": 5}) == {"count": 5}
    assert queue.claim() is None
    with pytest.raises(LeaseError, match="differing result"):
        queue.complete(second.lease_id, {"count": 4})


def test_work_contract_attempt_and_wall_budgets_bound_redelivery():
    clock = FakeClock()
    contract = toy_contract()
    contract.budget.max_attempts = 2
    contract.budget.wall_seconds = 100
    queue = make_fixture(clock, contract=contract)
    first = queue.claim()
    queue.abandon(first.lease_id)
    second = queue.claim()
    queue.abandon(second.lease_id)
    assert queue.claim() is None

    short = toy_contract()
    short.budget.max_attempts = 5
    short.budget.wall_seconds = 5
    deadline_queue = make_fixture(clock, contract=short)
    delivery = deadline_queue.claim()
    assert delivery.lease_expires_at == 105.0
    clock.advance(5)
    assert deadline_queue.claim() is None
    with pytest.raises(LeaseError, match="expired"):
        deadline_queue.complete(delivery.lease_id, {"count": 5})


def test_duplicate_completion_survives_lost_response_and_lease_time():
    clock = FakeClock()
    queue = make_fixture(clock)
    delivery = queue.claim()
    queue.complete(delivery.lease_id, {"count": 5})
    clock.advance(1000)
    assert queue.complete(delivery.lease_id, {"count": 5}) == {"count": 5}
