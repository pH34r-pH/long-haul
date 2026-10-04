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
            if (
                self.record is None
                or self.record.id != self.decision_id
                or self.record.initial_positions
                or self.record.final_positions
                or self.record.resolution is not None
                or self.position is not None
                or self.resolution is not None
                or self.action is not None
                or self.authority_holder is not None
            ):
                raise ValueError("decision creation needs a matching unresolved empty record")
        elif self.operation in ("initial_captured", "final_captured"):
            if (
                self.position is None
                or self.record is not None
                or self.resolution is not None
                or self.action is not None
                or self.authority_holder is not None
            ):
                raise ValueError("position event needs only its typed position")
        elif (
            self.resolution is None
            or not self.action
            or not self.action.strip()
            or self.record is not None
            or self.position is not None
        ):
            raise ValueError("resolution event needs a resolution and nonempty action")
        return self


def materialize_decision(events: Sequence[Event], decision_id: str) -> DecisionRecord:
    """Rebuild one decision snapshot from canonical events, rejecting conflicting delivery."""
    seen: dict[str, str] = {}
    record: DecisionRecord | None = None
    engine = DecisionEngine()
    for index, event in enumerate(events):
        serialized = event.model_dump_json()
        previous = seen.get(event.id)
        if previous is not None:
            if previous != serialized:
                raise ValueError("conflicting duplicate event identity")
            continue
        seen[event.id] = serialized
        if event.event_type != "decision_lifecycle":
            continue
        transition = DecisionLifecycleEvent.model_validate(event.payload)
        if transition.decision_id != decision_id:
            continue
        if event.actor not in (record.participants if record else _participants(transition)):
            raise ValueError("decision event actor must be a participant")
        if not event.source:
            raise ValueError("decision event needs provenance source")
        if transition.operation == "created":
            if record is not None or transition.record is None:
                raise ValueError("decision history contains a repeated or malformed creation")
            record = transition.record
            continue
        if record is None:
            raise ValueError("decision transition precedes its creation event")
        if record.resolution is not None:
            raise ValueError("decision event follows its terminal resolution")
        if transition.operation in ("initial_captured", "final_captured"):
            if transition.position is None or transition.position.participant != event.actor:
                raise ValueError("position event actor must match its participant")
            if transition.operation == "initial_captured":
                record = engine.capture_initial(record, transition.position)
            else:
                record = engine.capture_final(record, transition.position)
            continue
        if transition.operation == "resolved":
            assert transition.resolution is not None and transition.action is not None
            record = engine.resolve_with_events(
                record,
                transition.resolution,
                transition.action,
                events[:index],
                transition.authority_holder,
            )
    if record is None:
        raise KeyError(f"decision {decision_id!r} is not present in event history")
    return record


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
