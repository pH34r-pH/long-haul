"""Small append-only JSONL event store; SQLite is intentionally not required for MVP."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from ..ephemeral import (
    AttemptStatus,
    AttemptTermination,
    MissionRun,
    MissionRunStatus,
    ResourceLease,
    WorkAttempt,
)
from ..models import CrewMember, Embodiment, Vessel

EventType = Literal[
    "observation", "self_report", "inference", "unknown", "request", "decision",
    "embodiment_change", "competence_update", "rupture", "repair",
    "resource_admitted", "resource_lease_renewed", "resource_disappeared",
    "work_contract_created", "work_attempt_started", "work_attempt_completed",
    "work_attempt_interrupted", "work_attempt_timed_out", "work_evidence",
    "work_disposition", "work_fact", "work_constraint", "work_subgoal",
    "work_failure", "work_failure_resolved", "work_artifact_verified",
    "evaluation_started", "evaluation_evidence", "evaluation_regression",
    "work_progress_intervention", "work_attempt_handed_back",
]
class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: EventType; actor: str | None = None; source: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict); schema_version: int = 1

class EventStore:
    def __init__(self, path: str | Path): self.path = Path(path)
    def append(self, event: Event) -> Event:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as out: out.write(event.model_dump_json() + "\n")
        return event
    def events(self) -> list[Event]:
        if not self.path.exists(): return []
        return [Event.model_validate_json(line) for line in self.path.read_text().splitlines() if line.strip()]
    def export_jsonl(self) -> str: return "".join(e.model_dump_json() + "\n" for e in self.events())

class FleetState(BaseModel):
    crew: dict[str, CrewMember] = Field(default_factory=dict)
    ruptures: list[dict[str, Any]] = Field(default_factory=list)
    vessels: dict[str, Vessel] = Field(default_factory=dict)
    unavailable_vessels: dict[str, str] = Field(default_factory=dict)
    attempts: dict[str, WorkAttempt] = Field(default_factory=dict)
    missions: dict[str, MissionRun] = Field(default_factory=dict)


    def active_vessels(self, now: datetime | None = None) -> list[Vessel]:
        """Return vessels admitted and currently covered by their lease."""
        instant = now or datetime.now(UTC)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("fleet snapshots require a timezone-aware time")
        return [
            vessel for vessel in self.vessels.values()
            if vessel.id not in self.unavailable_vessels
            and (vessel.lease is None or vessel.lease.active(instant))
        ]


def _mission(state: FleetState, mission_id: str) -> MissionRun:
    if mission_id not in state.missions:
        state.missions[mission_id] = MissionRun(mission_id=mission_id)
    return state.missions[mission_id]


def _interruption_reason(value: str) -> AttemptTermination:
    reason = AttemptTermination(value)
    if reason is AttemptTermination.EXECUTION_TIMEOUT:
        raise ValueError("execution timeouts require a work_attempt_timed_out event")
    return reason


def _end_attempt(
    state: FleetState,
    attempt_id: str,
    *,
    event: Event,
    status: AttemptStatus,
    termination: AttemptTermination | None,
) -> None:
    attempt = state.attempts.get(attempt_id)
    if attempt is None or attempt.status is not AttemptStatus.RUNNING:
        return
    attempt.status = status
    attempt.termination = termination
    attempt.ended_at = event.timestamp
    attempt.end_event_id = event.id
    mission = _mission(state, attempt.mission_id)
    mission.active_attempt_ids = [value for value in mission.active_attempt_ids if value != attempt_id]
    if mission.active_attempt_ids:
        mission.status = MissionRunStatus.ACTIVE
    elif status is AttemptStatus.COMPLETED:
        mission.status = MissionRunStatus.COMPLETED
    else:
        mission.status = MissionRunStatus.REPLAN_REQUIRED


def materialize(
    events: list[Event],
    initial_crew: list[CrewMember] | None = None,
    initial_vessels: list[Vessel] | None = None,
) -> FleetState:
    initial_crew = initial_crew or []
    initial_vessels = initial_vessels or []
    state = FleetState(
        crew={member.crew_id: member.model_copy(deep=True) for member in initial_crew},
        vessels={vessel.id: vessel.model_copy(deep=True) for vessel in initial_vessels},
    )
    for event in events:
        if event.event_type == "embodiment_change":
            member = state.crew[event.payload["crew_id"]]
            member.embodiment = Embodiment.model_validate(event.payload["embodiment"])
            member.generation = event.payload.get("generation", member.generation + 1)
        elif event.event_type == "competence_update":
            state.crew[event.payload["crew_id"]].competence = event.payload["competence"]
        elif event.event_type == "rupture": state.ruptures.append(event.payload)
        elif event.event_type == "repair": state.ruptures = [r for r in state.ruptures if r.get("id") != event.payload.get("rupture_id")]
        elif event.event_type == "resource_admitted":
            vessel = Vessel.model_validate(event.payload["vessel"])
            state.vessels[vessel.id] = vessel
            state.unavailable_vessels.pop(vessel.id, None)
        elif event.event_type == "resource_lease_renewed":
            vessel = state.vessels.get(event.payload["vessel_id"])
            if vessel is not None:
                vessel.lease = ResourceLease.model_validate(event.payload["lease"])
        elif event.event_type == "resource_disappeared":
            vessel_id = event.payload["vessel_id"]
            lease_id = event.payload["lease_id"]
            reason = _interruption_reason(event.payload["reason"])
            state.unavailable_vessels[vessel_id] = lease_id
            for attempt_id in event.payload.get("interrupted_attempt_ids", []):
                _end_attempt(
                    state,
                    attempt_id,
                    event=event,
                    status=AttemptStatus.INCONCLUSIVE,
                    termination=reason,
                )
        elif event.event_type == "work_attempt_started":
            attempt = WorkAttempt(
                attempt_id=event.payload["attempt_id"],
                mission_id=event.payload["mission_id"],
                vessel_id=event.payload["vessel_id"],
                lease_id=event.payload["lease_id"],
                restartable=event.payload.get("restartable", False),
                started_at=event.timestamp,
                start_event_id=event.id,
            )
            state.attempts[attempt.attempt_id] = attempt
            mission = _mission(state, attempt.mission_id)
            mission.status = MissionRunStatus.ACTIVE
            mission.attempt_ids.append(attempt.attempt_id)
            mission.active_attempt_ids.append(attempt.attempt_id)
        elif event.event_type == "work_attempt_interrupted":
            _end_attempt(
                state,
                event.payload["attempt_id"],
                event=event,
                status=AttemptStatus.INCONCLUSIVE,
                termination=_interruption_reason(event.payload["reason"]),
            )
        elif event.event_type == "work_attempt_timed_out":
            _end_attempt(
                state,
                event.payload["attempt_id"],
                event=event,
                status=AttemptStatus.TIMED_OUT,
                termination=AttemptTermination.EXECUTION_TIMEOUT,
            )
        elif event.event_type == "work_attempt_completed" and "attempt_id" in event.payload:
            _end_attempt(
                state,
                event.payload["attempt_id"],
                event=event,
                status=AttemptStatus.COMPLETED,
                termination=None,
            )
    return state
