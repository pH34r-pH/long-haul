"""Small append-only JSONL event store with streaming replay, locking, and hash-chain integrity."""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from ..ephemeral import (
    AttemptStatus,
    AttemptTermination,
    MissionRun,
    MissionRunStatus,
    ResourceLease,
    WorkAttempt,
)
from ..models import CrewMember, Embodiment, Vessel
from .integrity import (
    SUPPORTED_SCHEMA_VERSION,
    ChainCheckpoint,
    QuarantinedRecord,
    ReplayDiagnostic,
    ReplayError,
    ReplayResult,
    compute_event_hash,
    exclusive_file_lock,
    genesis_hash,
    iter_raw_jsonl_lines,
    verify_chain_records,
)

EventType = Literal[
    "observation", "self_report", "inference", "unknown", "request", "decision", "decision_lifecycle", "objection_lifecycle",
    "embodiment_change", "competence_update", "rupture", "repair",
    "resource_admitted", "resource_lease_renewed", "resource_disappeared",
    "work_contract_created", "work_attempt_started", "work_attempt_completed",
    "work_attempt_interrupted", "work_attempt_timed_out", "work_evidence",
    "work_disposition", "work_fact", "work_constraint", "work_subgoal",
    "work_failure", "work_failure_resolved", "work_artifact_verified", "work_repository_context",
    "evaluation_started", "evaluation_evidence", "evaluation_regression",
    "work_progress_intervention", "work_attempt_handed_back",
]
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(get_args(EventType))


class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    event_type: EventType
    actor: str | None = None
    source: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = SUPPORTED_SCHEMA_VERSION
    log_id: str | None = None
    seq: int | None = None
    prev_hash: str | None = None
    event_hash: str | None = None


def _validate_append_history(path: Path, existing: ReplayResult, event: Event) -> None:
    if existing.diagnostics:
        raise ReplayError(existing.diagnostics[0])
    if existing.quarantined:
        item = existing.quarantined[0]
        raise ReplayError(ReplayDiagnostic(str(path), item.line, item.offset, item.reason))
    if any(item.id == event.id for item in existing.events):
        raise ValueError("duplicate event identity")


def _classify_parsed_record(
    path: Path,
    line_no: int,
    offset: int,
    text: str,
    parsed: dict[str, Any],
) -> tuple[Event | None, QuarantinedRecord | None]:
    raw_type = parsed.get("event_type")
    schema_version = parsed.get("schema_version", SUPPORTED_SCHEMA_VERSION)
    event_id = str(parsed.get("id")) if parsed.get("id") is not None else None
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise ReplayError(ReplayDiagnostic(str(path), line_no, offset, "invalid schema_version type", event_id))
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        reason = f"unsupported schema_version {schema_version} (supported: {SUPPORTED_SCHEMA_VERSION})"
        return None, QuarantinedRecord(line_no, offset, text, parsed, reason)
    if not isinstance(raw_type, str) or raw_type not in KNOWN_EVENT_TYPES:
        reason = f"unknown event_type: {raw_type!r}"
        return None, QuarantinedRecord(line_no, offset, text, parsed, reason)
    try:
        return Event.model_validate(parsed), None
    except ValidationError as exc:
        raise ReplayError(
            ReplayDiagnostic(str(path), line_no, offset, f"invalid event record: {exc.errors()[0]['msg']}", event_id, raw_type, schema_version)
        ) from exc


class EventStore:
    def __init__(self, path: str | Path, *, lock_timeout_s: float = 5.0, chain_integrity: bool = True) -> None:
        self.path = Path(path)
        self.lock_timeout_s = lock_timeout_s
        self.chain_integrity = chain_integrity

    def _scan_unlocked(self, *, anchor: ChainCheckpoint | None = None) -> tuple[ReplayResult, list[tuple[int, int, dict[str, Any]]]]:
        result = ReplayResult()
        parsed_records: list[tuple[int, int, dict[str, Any]]] = []
        for line_no, offset, text, ended_with_nl, is_last in iter_raw_jsonl_lines(self.path):
            if not text.strip():
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                diag = ReplayDiagnostic(str(self.path), line_no, offset, f"malformed JSON: {exc.msg}")
                if is_last and not ended_with_nl:
                    result.incomplete_tail = diag
                    break
                raise ReplayError(diag) from exc
            if not isinstance(parsed, dict):
                raise ReplayError(ReplayDiagnostic(str(self.path), line_no, offset, "event record must be a JSON object"))
            parsed_records.append((line_no, offset, parsed))
            event, quarantined = _classify_parsed_record(self.path, line_no, offset, text, parsed)
            if quarantined is not None:
                result.quarantined.append(quarantined)
                result.diagnostics.append(
                    ReplayDiagnostic(
                        str(self.path), line_no, offset, quarantined.reason,
                        event_id=str(parsed.get("id")) if parsed.get("id") is not None else None,
                        raw_type=str(parsed.get("event_type")) if parsed.get("event_type") is not None else None,
                        schema_version=parsed.get("schema_version") if isinstance(parsed.get("schema_version"), int) else None,
                    )
                )
            elif event is not None:
                result.events.append(event)
        status, checkpoint = verify_chain_records(self.path, parsed_records, anchor=anchor)
        result.integrity_status = status
        result.checkpoint = checkpoint
        return result, parsed_records

    def append(self, event: Event) -> Event:
        return self._append(event, None)

    def append_validated(self, event: Event, validate: Callable[[list[Event], Event], None]) -> Event:
        """Validate under the writer lock; the caller must provide trusted policy.

        This serializes cooperative writers but does not authenticate a validator
        or prevent raw writes, file modification, or a caller-supplied no-op.
        """
        return self._append(event, validate)

    def _append(self, event: Event, validate: Callable[[list[Event], Event], None] | None) -> Event:
        if event.schema_version != SUPPORTED_SCHEMA_VERSION:
            raise ReplayError(
                ReplayDiagnostic(str(self.path), 0, 0, f"cannot append unsupported schema_version {event.schema_version}", event.id)
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.path, timeout_s=self.lock_timeout_s):
            existing, parsed_records = self._scan_unlocked()
            if existing.incomplete_tail is not None:
                raise ReplayError(existing.incomplete_tail)
            if validate is not None:
                _validate_append_history(self.path, existing, event)
            record = event.model_copy(deep=True)
            if self.chain_integrity:
                if existing.checkpoint is not None:
                    log_id = existing.checkpoint.log_id
                    next_seq = existing.checkpoint.seq + 1
                    prev_hash = existing.checkpoint.head_hash
                elif parsed_records:
                    # Existing legacy unchained log: preserve compatibility without rewriting history.
                    log_id = None
                    next_seq = None
                    prev_hash = None
                else:
                    log_id = str(uuid4())
                    next_seq = 1
                    prev_hash = genesis_hash(log_id)
                if log_id is not None and next_seq is not None and prev_hash is not None:
                    record.log_id = log_id
                    record.seq = next_seq
                    record.prev_hash = prev_hash
                    record.event_hash = None
                    dumped = json.loads(record.model_dump_json())
                    record.event_hash = compute_event_hash(dumped)
            if validate is not None:
                validated_bytes = record.model_dump_json()
                validate(existing.events, record)
                if record.model_dump_json() != validated_bytes:
                    raise ValueError("validator mutated candidate event")
            serialized = record.model_dump_json() + "\n"
            try:
                with self.path.open("a", encoding="utf-8", newline="\n") as out:
                    out.write(serialized)
                    out.flush()
                    os.fsync(out.fileno())
            except OSError as exc:
                raise ReplayError(ReplayDiagnostic(str(self.path), 0, 0, f"durable append failed: {exc}", record.id)) from exc
            return record

    def read_replay(self, *, strict: bool = False, anchor: ChainCheckpoint | None = None) -> ReplayResult:
        """Stream and verify the log without mutating it.

        When ``strict=True``, any torn tail, unknown event type, or unsupported
        schema version raises ``ReplayError``.
        """
        result, _ = self._scan_unlocked(anchor=anchor)
        if strict:
            if result.incomplete_tail is not None:
                raise ReplayError(result.incomplete_tail)
            if result.diagnostics:
                raise ReplayError(result.diagnostics[0])
        return result

    def verify_chain(self, *, anchor: ChainCheckpoint | None = None) -> ReplayResult:
        return self.read_replay(strict=False, anchor=anchor)

    def checkpoint(self) -> ChainCheckpoint | None:
        return self.read_replay(strict=False).checkpoint

    def recover_torn_tail(self, *, backup_path: Path | None = None) -> ReplayDiagnostic | None:
        """Explicitly truncate an unterminated torn trailing fragment after preserving original bytes."""
        with exclusive_file_lock(self.path, timeout_s=self.lock_timeout_s):
            result, _ = self._scan_unlocked()
            if result.incomplete_tail is None:
                return None
            backup = backup_path or self.path.with_suffix(self.path.suffix + ".damaged")
            shutil.copy2(self.path, backup)
            with self.path.open("r+b") as handle:
                handle.truncate(result.incomplete_tail.offset)
                handle.flush()
                os.fsync(handle.fileno())
            return result.incomplete_tail

    def events(self, *, strict: bool = True) -> list[Event]:
        return list(self.read_replay(strict=strict).events)

    def export_jsonl(self) -> str:
        replay = self.read_replay(strict=False)
        lines = [e.model_dump_json() for e in replay.events]
        lines.extend(q.raw_line for q in replay.quarantined)
        return "".join(line + "\n" for line in lines)

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


def _apply_embodiment_change(state: FleetState, event: Event) -> None:
    crew_id = event.payload.get("crew_id")
    if not isinstance(crew_id, str) or crew_id not in state.crew:
        raise ReplayError(
            ReplayDiagnostic("<replay>", 0, 0, f"embodiment_change references unknown crew member: {crew_id!r}", event.id, event.event_type, event.schema_version)
        )
    member = state.crew[crew_id]
    member.embodiment = Embodiment.model_validate(event.payload["embodiment"])
    member.generation = event.payload.get("generation", member.generation + 1)


def _apply_competence_update(state: FleetState, event: Event) -> None:
    crew_id = event.payload.get("crew_id")
    if not isinstance(crew_id, str) or crew_id not in state.crew:
        raise ReplayError(
            ReplayDiagnostic("<replay>", 0, 0, f"competence_update references unknown crew member: {crew_id!r}", event.id, event.event_type, event.schema_version)
        )
    state.crew[crew_id].competence = event.payload["competence"]


def _apply_rupture(state: FleetState, event: Event) -> None:
    state.ruptures.append(event.payload)


def _apply_repair(state: FleetState, event: Event) -> None:
    rupture_id = event.payload.get("rupture_id")
    state.ruptures = [rupture for rupture in state.ruptures if rupture.get("id") != rupture_id]


def _apply_resource_admitted(state: FleetState, event: Event) -> None:
    vessel = Vessel.model_validate(event.payload["vessel"])
    state.vessels[vessel.id] = vessel
    state.unavailable_vessels.pop(vessel.id, None)


def _apply_resource_lease_renewed(state: FleetState, event: Event) -> None:
    vessel = state.vessels.get(event.payload["vessel_id"])
    if vessel is not None:
        vessel.lease = ResourceLease.model_validate(event.payload["lease"])


def _apply_resource_disappeared(state: FleetState, event: Event) -> None:
    vessel_id = event.payload["vessel_id"]
    state.unavailable_vessels[vessel_id] = event.payload["lease_id"]
    reason = _interruption_reason(event.payload["reason"])
    for attempt_id in event.payload.get("interrupted_attempt_ids", []):
        _end_attempt(
            state,
            attempt_id,
            event=event,
            status=AttemptStatus.INCONCLUSIVE,
            termination=reason,
        )


def _apply_work_attempt_started(state: FleetState, event: Event) -> None:
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


def _apply_work_attempt_interrupted(state: FleetState, event: Event) -> None:
    _end_attempt(
        state,
        event.payload["attempt_id"],
        event=event,
        status=AttemptStatus.INCONCLUSIVE,
        termination=_interruption_reason(event.payload["reason"]),
    )


def _apply_work_attempt_timed_out(state: FleetState, event: Event) -> None:
    _end_attempt(
        state,
        event.payload["attempt_id"],
        event=event,
        status=AttemptStatus.TIMED_OUT,
        termination=AttemptTermination.EXECUTION_TIMEOUT,
    )


def _apply_work_attempt_completed(state: FleetState, event: Event) -> None:
    if "attempt_id" not in event.payload:
        return
    _end_attempt(
        state,
        event.payload["attempt_id"],
        event=event,
        status=AttemptStatus.COMPLETED,
        termination=None,
    )


_EVENT_HANDLERS: dict[str, Callable[[FleetState, Event], None]] = {
    "embodiment_change": _apply_embodiment_change,
    "competence_update": _apply_competence_update,
    "rupture": _apply_rupture,
    "repair": _apply_repair,
    "resource_admitted": _apply_resource_admitted,
    "resource_lease_renewed": _apply_resource_lease_renewed,
    "resource_disappeared": _apply_resource_disappeared,
    "work_attempt_started": _apply_work_attempt_started,
    "work_attempt_interrupted": _apply_work_attempt_interrupted,
    "work_attempt_timed_out": _apply_work_attempt_timed_out,
    "work_attempt_completed": _apply_work_attempt_completed,
}


def _apply_event(state: FleetState, event: Event) -> None:
    if event.schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ReplayError(
            ReplayDiagnostic(
                "<replay>", 0, 0,
                f"unsupported schema_version {event.schema_version} (supported: {SUPPORTED_SCHEMA_VERSION})",
                event.id, event.event_type, event.schema_version,
            )
        )
    handler = _EVENT_HANDLERS.get(event.event_type)
    if handler is not None:
        handler(state, event)


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
        _apply_event(state, event)
    return state
