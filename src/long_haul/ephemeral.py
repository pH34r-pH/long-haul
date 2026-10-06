"""Provider-neutral lifecycle and lease semantics for short-lived vessels.

Lease metadata describes compute ownership and availability. Long Haul's event
store remains the source of truth for mission and attempt continuity.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    from .events.store import EventStore, FleetState
    from .models import Vessel


class VesselLifecycle(str, Enum):
    PERSISTENT = "persistent"
    EPHEMERAL = "ephemeral"
    EXTERNAL = "external"


class AttemptStatus(str, Enum):
    RUNNING = "running"
    INCONCLUSIVE = "inconclusive"
    TIMED_OUT = "timed_out"
    COMPLETED = "completed"


class AttemptTermination(str, Enum):
    LEASE_LOST = "lease_lost"
    RUNNER_INTERRUPTED = "runner_interrupted"
    EXECUTION_TIMEOUT = "execution_timeout"


class MissionRunStatus(str, Enum):
    ACTIVE = "active"
    REPLAN_REQUIRED = "replan_required"
    COMPLETED = "completed"


class ResourceLease(BaseModel):
    """An externally owned availability window, independent of task timeout."""

    id: str = Field(min_length=1)
    acquired_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    preemptible: bool = True
    renewable: bool = False
    trust_domain: str = Field(default="external", min_length=1)
    # Describes the host/lease only; it never moves canonical Long Haul state.
    durable_state: bool = False
    cost_metadata: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_lease(self) -> ResourceLease:
        if self.acquired_at.tzinfo is None or self.acquired_at.utcoffset() is None:
            raise ValueError("lease acquired_at must be timezone-aware")
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
                raise ValueError("lease expires_at must be timezone-aware")
            if self.expires_at <= self.acquired_at:
                raise ValueError("lease expires_at must be after acquired_at")
        if self.cost_metadata is not None:
            json.dumps(self.cost_metadata, sort_keys=True, allow_nan=False)
        return self

    def active(self, now: datetime | None = None) -> bool:
        """Return whether the lease covers ``now`` (expiry is exclusive)."""
        instant = now or datetime.now(UTC)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("lease checks require a timezone-aware time")
        return self.acquired_at <= instant and (self.expires_at is None or instant < self.expires_at)


class WorkAttempt(BaseModel):
    attempt_id: str
    mission_id: str
    vessel_id: str
    lease_id: str
    restartable: bool = False
    status: AttemptStatus = AttemptStatus.RUNNING
    termination: AttemptTermination | None = None
    started_at: datetime
    ended_at: datetime | None = None
    start_event_id: str
    end_event_id: str | None = None


class MissionRun(BaseModel):
    mission_id: str
    status: MissionRunStatus = MissionRunStatus.ACTIVE
    attempt_ids: list[str] = Field(default_factory=list)
    active_attempt_ids: list[str] = Field(default_factory=list)


class LeaseLifecycleError(ValueError):
    """The requested operation does not match current fleet lease state."""


def _instant(value: datetime | None) -> datetime:
    instant = value or datetime.now(UTC)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("lease lifecycle timestamps must be timezone-aware")
    return instant


class FleetLeaseController:
    """Append lease and attempt transitions to the durable event store.

    This controller knows only the normalized Long Haul contract. Provider
    adapters can translate their lifecycle notifications into these methods.
    """

    def __init__(self, event_store: EventStore):
        self.event_store = event_store

    def _append(self, event_type: str, payload: dict[str, Any], at: datetime):
        from .events.store import Event

        return self.event_store.append(Event(
            timestamp=at,
            event_type=event_type,
            source="fleet-lease-controller",
            payload=payload,
        ))

    def state(self) -> FleetState:
        from .events.store import materialize

        return materialize(self.event_store.events())

    def admit(self, vessel: Vessel, *, at: datetime | None = None):
        """Record a leased ephemeral or externally managed vessel joining."""
        instant = _instant(at)
        if vessel.lifecycle is VesselLifecycle.PERSISTENT or vessel.lease is None:
            raise LeaseLifecycleError("admission requires an ephemeral or external vessel with a lease")
        if not vessel.lease.active(instant):
            raise LeaseLifecycleError("cannot admit a vessel under an inactive lease")
        current = self.state()
        if vessel.id in current.vessels and vessel.id not in current.unavailable_vessels:
            raise LeaseLifecycleError("vessel is already admitted")
        return self._append(
            "resource_admitted",
            {"vessel": vessel.model_dump(mode="json")},
            instant,
        )

    def renew(
        self,
        vessel_id: str,
        lease_id: str,
        expires_at: datetime,
        *,
        at: datetime | None = None,
    ):
        """Append a renewal only when the current lease permits renewal."""
        from .events.store import materialize

        instant = _instant(at)
        end = _instant(expires_at)
        current = materialize(self.event_store.events())
        vessel = current.vessels.get(vessel_id)
        lease = vessel.lease if vessel else None
        if vessel_id in current.unavailable_vessels or lease is None or lease.id != lease_id:
            raise LeaseLifecycleError("lease is stale or vessel is unavailable")
        if not lease.renewable:
            raise LeaseLifecycleError("lease does not support renewal")
        if not lease.active(instant) or end <= instant:
            raise LeaseLifecycleError("lease cannot be renewed after expiry")
        if lease.expires_at is not None and end <= lease.expires_at:
            raise LeaseLifecycleError("renewed expiry must extend the current lease")
        renewed = lease.model_copy(update={"expires_at": end})
        return self._append(
            "resource_lease_renewed",
            {"vessel_id": vessel_id, "lease": renewed.model_dump(mode="json")},
            instant,
        )

    def start_attempt(
        self,
        *,
        attempt_id: str,
        mission_id: str,
        vessel_id: str,
        lease_id: str,
        restartable: bool = False,
        at: datetime | None = None,
    ):
        """Admit work only against a currently available vessel lease."""
        from .events.store import materialize

        instant = _instant(at)
        current = materialize(self.event_store.events())
        vessel = current.vessels.get(vessel_id)
        lease = vessel.lease if vessel else None
        if vessel_id in current.unavailable_vessels or lease is None or lease.id != lease_id:
            raise LeaseLifecycleError("attempt vessel has no matching available lease")
        if not lease.active(instant):
            raise LeaseLifecycleError("attempt vessel lease is expired or not yet active")
        if attempt_id in current.attempts:
            raise LeaseLifecycleError("attempt id has already been used")
        return self._append(
            "work_attempt_started",
            {
                "attempt_id": attempt_id,
                "mission_id": mission_id,
                "vessel_id": vessel_id,
                "lease_id": lease_id,
                "restartable": restartable,
            },
            instant,
        )

    def disappear(
        self,
        vessel_id: str,
        lease_id: str,
        *,
        reason: AttemptTermination = AttemptTermination.LEASE_LOST,
        at: datetime | None = None,
    ):
        """Record lease loss and mark its running attempts inconclusive."""
        from .events.store import materialize

        instant = _instant(at)
        if reason not in {AttemptTermination.LEASE_LOST, AttemptTermination.RUNNER_INTERRUPTED}:
            raise LeaseLifecycleError("resource disappearance must be lease_lost or runner_interrupted")
        current = materialize(self.event_store.events())
        vessel = current.vessels.get(vessel_id)
        lease = vessel.lease if vessel else None
        if vessel_id in current.unavailable_vessels or lease is None or lease.id != lease_id:
            raise LeaseLifecycleError("lease is stale or vessel is already unavailable")
        interrupted = [
            attempt.attempt_id
            for attempt in current.attempts.values()
            if attempt.status is AttemptStatus.RUNNING
            and attempt.vessel_id == vessel_id
            and attempt.lease_id == lease_id
        ]
        return self._append(
            "resource_disappeared",
            {
                "vessel_id": vessel_id,
                "lease_id": lease_id,
                "reason": reason.value,
                "interrupted_attempt_ids": interrupted,
            },
            instant,
        )

    def record_execution_timeout(self, attempt_id: str, *, at: datetime | None = None):
        """Record a task execution timeout separately from loss of its host."""
        from .events.store import materialize

        instant = _instant(at)
        current = materialize(self.event_store.events())
        attempt = current.attempts.get(attempt_id)
        if attempt is None or attempt.status is not AttemptStatus.RUNNING:
            raise LeaseLifecycleError("only a running attempt can time out")
        return self._append(
            "work_attempt_timed_out",
            {"attempt_id": attempt_id, "reason": AttemptTermination.EXECUTION_TIMEOUT.value},
            instant,
        )
