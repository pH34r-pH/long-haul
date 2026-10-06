"""Event-backed decision positions using the existing append-only EventStore."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from ..events.store import Event, EventStore
from ..models import DecisionPosition
from .engine import DecisionEngine, DecisionRecord, Resolution


class DecisionLifecycleEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    operation: Literal["created", "initial_captured", "final_captured", "resolved"]
    decision_id: str
    record: DecisionRecord | None = None
    position: DecisionPosition | None = None
    resolution: Resolution | None = None
    action: str | None = None
    authority_holder: str | None = None

    @model_validator(mode="after")
    def valid_operation_payload(self) -> DecisionLifecycleEvent:
        if self.operation == "created":
            _validate_creation_payload(self)
        elif self.operation in ("initial_captured", "final_captured"):
            _validate_position_payload(self)
        else:
            _validate_resolution_payload(self)
        return self


def _validate_creation_payload(event: DecisionLifecycleEvent) -> None:
    record = event.record
    invalid_record = (
        record is None
        or record.id != event.decision_id
        or record.initial_positions
        or record.final_positions
        or record.resolution is not None
    )
    extra_payload = any(
        value is not None
        for value in (event.position, event.resolution, event.action, event.authority_holder)
    )
    if invalid_record or extra_payload:
        raise ValueError("decision creation needs a matching unresolved empty record")


def _validate_position_payload(event: DecisionLifecycleEvent) -> None:
    position_missing = event.position is None
    extra_payload = any(
        value is not None
        for value in (event.record, event.resolution, event.action, event.authority_holder)
    )
    if position_missing or extra_payload:
        raise ValueError("position event needs only its typed position")


def _validate_resolution_payload(event: DecisionLifecycleEvent) -> None:
    invalid_action = not event.action or not event.action.strip()
    extra_payload = event.record is not None or event.position is not None
    if event.resolution is None or invalid_action or extra_payload:
        raise ValueError("resolution event needs a resolution and nonempty action")


def materialize_decision(events: Sequence[Event], decision_id: str) -> DecisionRecord:
    """Rebuild one decision snapshot from canonical events, rejecting conflicting delivery."""
    seen: dict[str, str] = {}
    record: DecisionRecord | None = None
    engine = DecisionEngine()
    for index, event in enumerate(events):
        if not _accept_event(seen, event):
            continue
        if event.event_type != "decision_lifecycle":
            continue
        transition = DecisionLifecycleEvent.model_validate(event.payload)
        if transition.decision_id != decision_id:
            continue
        record = _advance_decision(
            record, event, transition, events[:index], engine
        )
    if record is None:
        raise KeyError(f"decision {decision_id!r} is not present in event history")
    return record


def _accept_event(seen: dict[str, str], event: Event) -> bool:
    serialized = event.model_dump_json()
    previous = seen.get(event.id)
    if previous is not None:
        if previous != serialized:
            raise ValueError("conflicting duplicate event identity")
        return False
    seen[event.id] = serialized
    return True


def _advance_decision(
    record: DecisionRecord | None,
    event: Event,
    transition: DecisionLifecycleEvent,
    history: Sequence[Event],
    engine: DecisionEngine,
) -> DecisionRecord:
    _validate_event_envelope(record, event, transition)
    if transition.operation == "created":
        if record is not None or transition.record is None:
            raise ValueError("decision history contains a repeated or malformed creation")
        return transition.record
    if record is None:
        raise ValueError("decision transition precedes its creation event")
    if record.resolution is not None:
        raise ValueError("decision event follows its terminal resolution")
    if transition.operation in ("initial_captured", "final_captured"):
        return _capture_position(record, event, transition, engine)
    return _resolve_decision(record, transition, history, engine)


def _validate_event_envelope(
    record: DecisionRecord | None,
    event: Event,
    transition: DecisionLifecycleEvent,
) -> None:
    participants = record.participants if record else _participants(transition)
    if event.actor not in participants:
        raise ValueError("decision event actor must be a participant")
    if not event.source:
        raise ValueError("decision event needs provenance source")


def _capture_position(
    record: DecisionRecord,
    event: Event,
    transition: DecisionLifecycleEvent,
    engine: DecisionEngine,
) -> DecisionRecord:
    position = transition.position
    if position is None or position.participant != event.actor:
        raise ValueError("position event actor must match its participant")
    if transition.operation == "initial_captured":
        return engine.capture_initial(record, position)
    return engine.capture_final(record, position)


def _resolve_decision(
    record: DecisionRecord,
    transition: DecisionLifecycleEvent,
    history: Sequence[Event],
    engine: DecisionEngine,
) -> DecisionRecord:
    assert transition.resolution is not None and transition.action is not None
    return engine.resolve_with_events(
        record,
        transition.resolution,
        transition.action,
        history,
        transition.authority_holder,
    )


def _participants(transition: DecisionLifecycleEvent) -> frozenset[str]:
    return transition.record.participants if transition.record is not None else frozenset()


class DecisionLifecycle:
    """Append and rebuild decision state through the existing EventStore."""

    def __init__(self, store: EventStore, decision_id: str):
        self.store = store
        self.decision_id = decision_id
        materialize_decision(store.events(), decision_id)

    @classmethod
    def create(
        cls,
        store: EventStore,
        record: DecisionRecord,
        *,
        actor: str,
        source: str,
    ) -> DecisionLifecycle:
        if record.initial_positions or record.final_positions or record.resolution is not None:
            raise ValueError("create from an unresolved record with no captured positions")
        if actor not in record.participants:
            raise ValueError("decision creator must be a participant")
        if not source:
            raise ValueError("decision creation needs provenance source")
        for event in store.events():
            if event.event_type == "decision_lifecycle":
                transition = DecisionLifecycleEvent.model_validate(event.payload)
                if transition.decision_id == record.id:
                    raise ValueError("decision identity already exists in event history")
        transition = DecisionLifecycleEvent(operation="created", decision_id=record.id, record=record)
        store.append(
            Event(
                event_type="decision_lifecycle",
                actor=actor,
                source=source,
                payload=transition.model_dump(mode="json"),
            )
        )
        return cls(store, record.id)

    @property
    def record(self) -> DecisionRecord:
        return materialize_decision(self.store.events(), self.decision_id)

    def capture_initial(self, position: DecisionPosition, *, actor: str, source: str) -> DecisionRecord:
        if actor != position.participant:
            raise ValueError("initial position must be captured by its participant")
        next_record = DecisionEngine().capture_initial(self.record, position)
        self._append(
            DecisionLifecycleEvent(
                operation="initial_captured", decision_id=self.decision_id, position=position
            ),
            actor,
            source,
        )
        return next_record

    def capture_final(self, position: DecisionPosition, *, actor: str, source: str) -> DecisionRecord:
        if actor != position.participant:
            raise ValueError("final position must be captured by its participant")
        next_record = DecisionEngine().capture_final(self.record, position)
        self._append(
            DecisionLifecycleEvent(
                operation="final_captured", decision_id=self.decision_id, position=position
            ),
            actor,
            source,
        )
        return next_record

    def resolve(
        self,
        resolution: Resolution,
        action: str,
        *,
        actor: str,
        source: str,
        authority_holder: str | None = None,
    ) -> DecisionRecord:
        current = self.record
        if actor not in current.participants:
            raise ValueError("resolution event actor must be a participant")
        if not source:
            raise ValueError("resolution event needs provenance source")
        next_record = DecisionEngine().resolve_with_events(
            current,
            resolution,
            action,
            self.store.events(),
            authority_holder,
        )
        self._append(
            DecisionLifecycleEvent(
                operation="resolved",
                decision_id=self.decision_id,
                resolution=resolution,
                action=action,
                authority_holder=authority_holder,
            ),
            actor,
            source,
        )
        return next_record

    def _append(self, transition: DecisionLifecycleEvent, actor: str, source: str) -> None:
        if actor not in self.record.participants:
            raise ValueError("decision event actor must be a participant")
        if not source:
            raise ValueError("decision event needs provenance source")
        self.store.append(
            Event(
                event_type="decision_lifecycle",
                actor=actor,
                source=source,
                payload=transition.model_dump(mode="json"),
            )
        )
