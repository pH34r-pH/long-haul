import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from long_haul.work import (
    FlashDeliveryFixture,
    FlashExecutionExtensionV1,
    LeaseError,
    WorkContract,
)

FIXTURE = Path(__file__).parent / "fixtures/work/flash-f0-toy.json"


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_fixture(clock):
    return FlashDeliveryFixture(
        job_id="toy-job-1", contract_id="toy-f0-contract-v1", lease_seconds=10,
        clock=clock, id_factory=lambda value: value,
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
    second = queue.claim()
    assert second.attempt_id != first.attempt_id
    assert second.lease_id != first.lease_id
    with pytest.raises(LeaseError, match="stale"):
        queue.renew(first.lease_id)
    with pytest.raises(LeaseError, match="stale"):
        queue.complete(first.lease_id, {"count": 5})
    assert queue.renew(second.lease_id).lease_expires_at == 120.0


def test_abandon_redelivers_and_completion_is_idempotent():
    queue = make_fixture(FakeClock())
    first = queue.claim()
    queue.abandon(first.lease_id)
    second = queue.claim()
    assert second.attempt_id != first.attempt_id
    expected = {"count": 5}
    assert queue.complete(second.lease_id, expected) == expected
    assert queue.complete(second.lease_id, expected) == expected
    assert queue.claim() is None
    with pytest.raises(LeaseError, match="differing result"):
        queue.complete(second.lease_id, {"count": 4})
