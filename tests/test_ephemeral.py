from datetime import UTC, datetime, timedelta

import pytest

from long_haul.ephemeral import (
    AttemptStatus,
    AttemptTermination,
    FleetLeaseController,
    LeaseLifecycleError,
    MissionRunStatus,
    ResourceLease,
    VesselLifecycle,
)
from long_haul.events import Event, EventStore, materialize
from long_haul.models import (
    ExecutionMode,
    InferenceProfile,
    ModelArtifact,
    Resource,
    Vessel,
)
from long_haul.runtime import ProfileValidation, RuntimeIdentity
from long_haul.scheduler import MissionRequirements, Scheduler
from long_haul.work.checkpoints import materialize_checkpoint

START = datetime(2026, 10, 1, tzinfo=UTC)
RUNTIME = RuntimeIdentity(runtime_id="runtime", version="1", build_id="synthetic", backends=["cpu"])


def leased_vessel(vessel_id, resource_id, *, expires_at=None, preemptible=True, renewable=False):
    lease = ResourceLease(
        id=f"lease-{vessel_id}",
        acquired_at=START,
        expires_at=expires_at,
        preemptible=preemptible,
        renewable=renewable,
        trust_domain="synthetic-ci",
        durable_state=False,
        cost_metadata={"currency": "USD", "rate": 0},
    )
    vessel = Vessel(
        id=vessel_id,
        name=vessel_id,
        class_name="station",
        resources=[Resource(id=resource_id, kind="cpu", memory_mb=1024)],
        lifecycle=VesselLifecycle.EXTERNAL,
        lease=lease,
    )
    return vessel


def candidate_profile(profile_id, resource_id):
    profile = InferenceProfile(
        id=profile_id,
        runtime_id=RUNTIME.runtime_id,
        strategy="resident",
        artifact=ModelArtifact(foundation="synthetic"),
        participating_resources=[resource_id],
    )
    validation = ProfileValidation(
        runtime=RUNTIME,
        profile_id=profile.id,
        artifact_key=profile.artifact.key,
        resources=profile.participating_resources,
        strategy=profile.strategy,
        options=profile.options,
        state="SUPPORTED",
        rationale="synthetic test evidence",
    )
    return profile, validation


def test_lease_disappearance_is_replayed_as_inconclusive_and_replanned(tmp_path):
    store = EventStore(tmp_path / "fleet.jsonl")
    controller = FleetLeaseController(store)
    worker = leased_vessel("worker", "worker-cpu", expires_at=START + timedelta(minutes=5))
    backup = Vessel(
        id="backup",
        name="backup",
        class_name="station",
        resources=[Resource(id="backup-cpu", kind="cpu", memory_mb=1024)],
    )

    controller.admit(worker, at=START)
    controller.start_attempt(
        attempt_id="attempt-1",
        mission_id="mission-1",
        vessel_id=worker.id,
        lease_id=worker.lease.id,
        restartable=True,
        at=START + timedelta(seconds=1),
    )
    store.append(Event(
        timestamp=START + timedelta(seconds=2),
        event_type="work_fact",
        payload={"contract_id": "contract-1", "mission_id": "mission-1", "text": "checkpointed input set"},
    ))
    controller.disappear(
        worker.id,
        worker.lease.id,
        reason=AttemptTermination.RUNNER_INTERRUPTED,
        at=START + timedelta(seconds=3),
    )

    rebuilt = materialize(store.events())
    assert worker.id in rebuilt.unavailable_vessels
    assert [v.id for v in rebuilt.active_vessels(START + timedelta(seconds=4))] == []
    attempt = rebuilt.attempts["attempt-1"]
    assert attempt.status is AttemptStatus.INCONCLUSIVE
    assert attempt.termination is AttemptTermination.RUNNER_INTERRUPTED
    assert rebuilt.missions["mission-1"].status is MissionRunStatus.REPLAN_REQUIRED
    assert rebuilt.missions["mission-1"].attempt_ids == ["attempt-1"]
    checkpoint = materialize_checkpoint("contract-1", store.events())
    assert [fact.text for fact in checkpoint.facts] == ["checkpointed input set"]

    profile, validation = candidate_profile("backup-profile", "backup-cpu")
    scheduler = Scheduler(
        [*rebuilt.active_vessels(START + timedelta(seconds=4)), backup],
        validations=[validation],
        current_runtimes=[RUNTIME],
        now=START + timedelta(seconds=4),
    )
    selected = scheduler.select(
        MissionRequirements(id="mission-1", allow_modes={ExecutionMode.LOCAL}),
        [profile],
    )
    assert selected.eligible
    assert selected.plan.vessels == ["backup"]
    assert store.events()[0].event_type == "resource_admitted"


def test_scheduler_rejects_expired_lease_and_selects_active_vessel():
    expired = leased_vessel("expired", "expired-cpu", expires_at=START + timedelta(seconds=10))
    active = leased_vessel("active", "active-cpu", expires_at=START + timedelta(minutes=2), preemptible=False)
    expired_profile, expired_validation = candidate_profile("expired-profile", "expired-cpu")
    active_profile, active_validation = candidate_profile("active-profile", "active-cpu")
    scheduler = Scheduler(
        [expired, active],
        validations=[expired_validation, active_validation],
        current_runtimes=[RUNTIME],
        now=START + timedelta(seconds=10),
    )
    mission = MissionRequirements(id="mission", allow_modes={ExecutionMode.LOCAL})

    results = [
        scheduler.evaluate(mission, plan, {"expired-profile": expired_profile, "active-profile": active_profile}[plan.inference_profile_id])
        for plan in scheduler.generate([expired_profile, active_profile])
        if plan.mode is ExecutionMode.LOCAL
    ]
    expired_result = next(result for result in results if result.plan.inference_profile_id == "expired-profile")
    assert not expired_result.eligible
    assert any("lease" in reason and "expired" in reason for reason in expired_result.reasons)
    assert scheduler.select(mission, [expired_profile, active_profile]).plan.inference_profile_id == "active-profile"


def test_preemptible_capacity_is_preferred_for_restartable_work_and_forbidden_when_required():
    interruptible = leased_vessel("interruptible", "interruptible-cpu", preemptible=True)
    stable = Vessel(
        id="stable",
        name="stable",
        class_name="station",
        resources=[Resource(id="stable-cpu", kind="cpu", memory_mb=1024)],
    )
    preemptible_profile, preemptible_validation = candidate_profile("preemptible", "interruptible-cpu")
    stable_profile, stable_validation = candidate_profile("stable", "stable-cpu")
    scheduler = Scheduler(
        [interruptible, stable],
        validations=[preemptible_validation, stable_validation],
        current_runtimes=[RUNTIME],
        now=START + timedelta(seconds=1),
    )

    restartable = MissionRequirements(
        id="restartable",
        allow_modes={ExecutionMode.LOCAL},
        restartable=True,
        preemption_policy="prefer",
    )
    selected = scheduler.select(restartable, [preemptible_profile, stable_profile])
    assert selected.plan.inference_profile_id == "preemptible"

    continuous = MissionRequirements(
        id="continuous",
        allow_modes={ExecutionMode.LOCAL},
        restartable=False,
        preemption_policy="forbid",
    )
    rejected = scheduler.evaluate(
        continuous,
        next(plan for plan in scheduler.generate([preemptible_profile]) if plan.mode is ExecutionMode.LOCAL),
        preemptible_profile,
    )
    assert not rejected.eligible
    assert any("preemptible resource is forbidden" in reason for reason in rejected.reasons)
    with pytest.raises(ValueError, match="only be preferred for restartable"):
        MissionRequirements(id="invalid", preemption_policy="prefer")


def test_execution_timeout_is_separate_from_lease_loss(tmp_path):
    store = EventStore(tmp_path / "timeout.jsonl")
    controller = FleetLeaseController(store)
    worker = leased_vessel("worker", "worker-cpu")
    controller.admit(worker, at=START)
    controller.start_attempt(
        attempt_id="attempt-timeout",
        mission_id="mission-timeout",
        vessel_id=worker.id,
        lease_id=worker.lease.id,
        at=START + timedelta(seconds=1),
    )
    controller.record_execution_timeout("attempt-timeout", at=START + timedelta(seconds=2))

    attempt = materialize(store.events()).attempts["attempt-timeout"]
    assert attempt.status is AttemptStatus.TIMED_OUT
    assert attempt.termination is AttemptTermination.EXECUTION_TIMEOUT
    assert attempt.status is not AttemptStatus.INCONCLUSIVE
    assert store.events()[-1].event_type == "work_attempt_timed_out"


def test_lease_loss_is_inconclusive_and_keeps_its_own_reason(tmp_path):
    store = EventStore(tmp_path / "lease-loss.jsonl")
    controller = FleetLeaseController(store)
    worker = leased_vessel("worker", "worker-cpu")
    controller.admit(worker, at=START)
    controller.start_attempt(
        attempt_id="attempt-lease-loss",
        mission_id="mission-lease-loss",
        vessel_id=worker.id,
        lease_id=worker.lease.id,
        at=START + timedelta(seconds=1),
    )
    controller.disappear(worker.id, worker.lease.id, at=START + timedelta(seconds=2))

    attempt = materialize(store.events()).attempts["attempt-lease-loss"]
    assert attempt.status is AttemptStatus.INCONCLUSIVE
    assert attempt.termination is AttemptTermination.LEASE_LOST


def test_lease_renewal_requires_support_and_extends_expiry(tmp_path):
    store = EventStore(tmp_path / "renewal.jsonl")
    controller = FleetLeaseController(store)
    worker = leased_vessel(
        "worker",
        "worker-cpu",
        expires_at=START + timedelta(minutes=1),
        renewable=True,
    )
    controller.admit(worker, at=START)
    controller.renew(
        worker.id,
        worker.lease.id,
        START + timedelta(minutes=2),
        at=START + timedelta(seconds=30),
    )
    assert controller.state().vessels[worker.id].lease.expires_at == START + timedelta(minutes=2)

    nonrenewable = leased_vessel("fixed", "fixed-cpu", expires_at=START + timedelta(minutes=1))
    controller.admit(nonrenewable, at=START)
    with pytest.raises(LeaseLifecycleError, match="does not support renewal"):
        controller.renew(
            nonrenewable.id,
            nonrenewable.lease.id,
            START + timedelta(minutes=2),
            at=START + timedelta(seconds=30),
        )
