"""Deterministic decision policy over immutable position history."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..events.store import Event, EventStore
from ..models import DecisionPosition, Position
from .objections import Objection, ObjectionProjection, materialize_objections

Resolution = Literal["consensus", "consent", "scoped_authority", "experiment", "escalation", "recorded_disagreement"]
T = TypeVar("T")


def _positions_for(record: DecisionRecord) -> tuple[DecisionPosition, ...]:
    return (*record.initial_positions, *record.final_positions)


def _validate_position_participants(
    positions: Sequence[DecisionPosition], participants: frozenset[str]
) -> None:
    names = [position.participant for position in positions]
    if len(names) != len(set(names)):
        raise ValueError("duplicate participant in position phase")
    if any(name not in participants for name in names):
        raise ValueError("position participant is not part of the decision")


def _unresolved_blocks(
    positions: Sequence[DecisionPosition], resolved_ids: frozenset[str]
) -> list[DecisionPosition]:
    return [
        position
        for position in positions
        if position.position is Position.BLOCK and position.objection_id not in resolved_ids
    ]


def _unresolved_typed_objections(
    positions: Sequence[DecisionPosition], resolved_ids: frozenset[str]
) -> list[DecisionPosition]:
    return [
        position
        for position in positions
        if position.position is Position.OBJECT
        and position.category in ("factual", "safety", "resource")
        and position.objection_id not in resolved_ids
    ]


def _require_withdrawal_rationale(record: DecisionRecord, position: DecisionPosition) -> None:
    prior = next((p for p in record.final_positions if p.participant == position.participant), None)
    prior = prior or next((p for p in record.initial_positions if p.participant == position.participant), None)
    if (prior is not None and prior.position is Position.BLOCK and position.position is not Position.BLOCK
            and (not position.rationale or not position.rationale.strip())):
        raise ValueError("withdrawing or resolving a block requires an explicit rationale")


class DecisionRecord(BaseModel):
    """Immutable decision snapshot; initial and final positions are distinct."""

    model_config = ConfigDict(frozen=True)

    id: str
    proposal: str
    participants: frozenset[str]
    consequential: bool = True
    exception: str | None = None
    initial_positions: tuple[DecisionPosition, ...] = Field(default_factory=tuple)
    final_positions: tuple[DecisionPosition, ...] = Field(default_factory=tuple)
    resolution: Resolution | None = None
    action: str | None = None
    authority_holder: str | None = None
    authority_scope: str | None = None
    authority_scopes: tuple[tuple[str, tuple[str, ...]], ...] = Field(default_factory=tuple)
    # Derived only while replaying canonical objection events; direct resolution ignores caller values.
    resolved_objection_ids: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("authority_scopes", mode="before")
    @classmethod
    def normalize_authority_scopes(cls, value: object) -> object:
        if isinstance(value, Mapping):
            items = value.items()
        else:
            items = value or ()
        return tuple(sorted((str(holder), tuple(sorted(set(scopes)))) for holder, scopes in items))

    @field_validator("resolved_objection_ids", mode="before")
    @classmethod
    def normalize_resolved_objection_ids(cls, value: object) -> object:
        return tuple(sorted(set(value or ())))

    @model_validator(mode="after")
    def validate_positions(self) -> DecisionRecord:
        if not self.participants:
            raise ValueError("decision requires at least one participant")
        if self.resolution is None and self.resolved_objection_ids:
            raise ValueError("unresolved decision cannot carry resolved objection IDs")
        for positions in (self.initial_positions, self.final_positions):
            _validate_position_participants(positions, self.participants)
        positions = _positions_for(self)
        unresolved_blocks = _unresolved_blocks(positions, frozenset(self.resolved_objection_ids))
        if unresolved_blocks and self.resolution not in (None, "experiment", "escalation"):
            raise ValueError("an unresolved initial block cannot be bypassed")
        objections = _unresolved_typed_objections(positions, frozenset(self.resolved_objection_ids))
        if any(position.category == "safety" for position in objections) and self.resolution not in (
            None, "experiment", "escalation"
        ):
            raise ValueError("an unresolved safety objection cannot be bypassed")
        if objections and self.resolution not in (
            None, "experiment", "escalation", "recorded_disagreement", "scoped_authority"
        ):
            raise ValueError("an unresolved typed objection requires an explicit resolution mode")
        return self

    def scopes_for(self, participant: str) -> frozenset[str]:
        return frozenset(
            scope
            for holder, scopes in self.authority_scopes
            if holder == participant
            for scope in scopes
        )


class DecisionEngine:
    """Pure decision operations that return a new snapshot instead of mutating history."""

    def capture_initial(self, record: DecisionRecord, position: DecisionPosition) -> DecisionRecord:
        if record.resolution is not None:
            raise ValueError("cannot capture an initial position after resolution")
        if position.participant not in record.participants:
            raise ValueError("unknown participant")
        if any(existing.participant == position.participant for existing in record.initial_positions):
            raise ValueError("initial position already captured")
        return _updated(record, initial_positions=(*record.initial_positions, position))

    def reveal_initial_positions(self, record: DecisionRecord) -> tuple[DecisionPosition, ...]:
        if {p.participant for p in record.initial_positions} != record.participants:
            raise ValueError("cannot reveal peer positions before all required initial captures complete")
        return record.initial_positions

    def capture_final(self, record: DecisionRecord, position: DecisionPosition) -> DecisionRecord:
        if {p.participant for p in record.initial_positions} != record.participants:
            raise ValueError("final disposition requires complete initial capture")
        if record.resolution is not None:
            raise ValueError("cannot capture a final position after resolution")
        if position.participant not in record.participants:
            raise ValueError("unknown participant")
        _require_withdrawal_rationale(record, position)
        if any(existing.participant == position.participant for existing in record.final_positions):
            raise ValueError("final position already captured")
        return _updated(record, final_positions=(*record.final_positions, position))

    def record_final(self, record: DecisionRecord, position: DecisionPosition) -> DecisionRecord:
        return self.capture_final(record, position)

    def resolve(
        self,
        record: DecisionRecord,
        resolution: Resolution,
        action: str,
        authority_holder: str | None = None,
        *,
        event_store: EventStore | None = None,
    ) -> DecisionRecord:
        if not action or not action.strip():
            raise ValueError("decision resolution requires a nonempty action")
        self._validate_resolution(record, resolution, authority_holder, frozenset())
        resolved = _updated(record, resolution=resolution, action=action, authority_holder=authority_holder, resolved_objection_ids=())
        if event_store is not None:
            event_store.append(Event(event_type="decision", actor=authority_holder, source="decision_engine",
                                     payload={"record": resolved.model_dump(mode="json")}))
        return resolved

    def resolve_with_events(
        self,
        record: DecisionRecord,
        resolution: Resolution,
        action: str,
        events: Sequence[Event],
        authority_holder: str | None = None,
    ) -> DecisionRecord:
        """Resolve using the active objection view rebuilt from canonical events."""
        if not action or not action.strip():
            raise ValueError("decision resolution requires a nonempty action")
        projection = materialize_objections(events, record)
        _validate_position_objection_links(record, projection)
        active = projection.blocking_for_decision()
        _validate_active_objections(active, resolution)
        resolved = projection.terminally_disposed_ids()
        self._validate_resolution(record, resolution, authority_holder, resolved)
        return _updated(record, resolution=resolution, action=action, authority_holder=authority_holder, resolved_objection_ids=tuple(sorted(resolved)))

    @staticmethod
    def _validate_resolution(
        record: DecisionRecord,
        resolution: Resolution,
        authority_holder: str | None,
        resolved_objection_ids: frozenset[str] = frozenset(),
    ) -> None:
        if record.resolution is not None:
            raise ValueError("decision is already resolved")
        _validate_independent_positions(record)
        positions = _positions_for(record)
        _validate_block_resolution(positions, resolution, resolved_objection_ids)
        _validate_typed_resolution(positions, resolution, resolved_objection_ids)
        _validate_authority_resolution(record, resolution, authority_holder)

    def dispatch_consequential(self, record: DecisionRecord, executor: Callable[[DecisionRecord], T]) -> T:
        raise ValueError("consequential decision dispatch is not integrated; refusing execution")


def _updated(record: DecisionRecord, **changes: object) -> DecisionRecord:
    return DecisionRecord.model_validate({**record.model_dump(), **changes})


def replay_decisions(events: Sequence[Event]) -> dict[str, DecisionRecord]:
    decisions = {}
    for event in events:
        if event.event_type == "decision" and isinstance(event.payload.get("record"), dict):
            record = DecisionRecord.model_validate(event.payload["record"])
            if record.resolved_objection_ids:
                raise ValueError("legacy decision snapshot cannot establish objection dispositions")
            decisions[record.id] = record
    return decisions


def _validate_position_objection_links(record: DecisionRecord, projection: ObjectionProjection) -> None:
    for position in _positions_for(record):
        if position.objection_id is None:
            continue
        try:
            objection = projection.get(position.objection_id)
        except KeyError as exc:
            raise ValueError("position references an objection absent from decision history") from exc
        if objection.raised_by != position.participant:
            raise ValueError("position objection must be raised by that participant")
        if objection.category != position.category:
            raise ValueError("position category differs from its linked objection")
        if objection.authority_scope != position.authority_scope:
            raise ValueError("position authority scope differs from its linked objection")
        applicability = next(
            (item for item in objection.applicability_history if item.revision == position.applicability_revision),
            None,
        )
        if applicability is None:
            raise ValueError("position applicability revision is absent from objection history")
        if applicability.scope != position.applicability_scope:
            raise ValueError("position applicability scope differs from its linked objection history")


def _validate_active_objections(active: Sequence[Objection], resolution: Resolution) -> None:
    allowed = ("experiment", "escalation", "recorded_disagreement", "scoped_authority")
    if active and resolution not in allowed:
        raise ValueError("active objection cannot be bypassed")
    if any(item.category == "safety" for item in active) and resolution not in ("experiment", "escalation"):
        raise ValueError("active objection with safety category cannot be bypassed")


def _validate_independent_positions(record: DecisionRecord) -> None:
    if {position.participant for position in record.initial_positions} != record.participants:
        raise ValueError("decision requires initial positions from every participant")


def _validate_block_resolution(
    positions: Sequence[DecisionPosition],
    resolution: Resolution,
    resolved_ids: frozenset[str],
) -> None:
    if _unresolved_blocks(positions, resolved_ids) and resolution not in ("escalation", "experiment"):
        raise ValueError("unresolved block cannot be bypassed")


def _validate_typed_resolution(
    positions: Sequence[DecisionPosition],
    resolution: Resolution,
    resolved_ids: frozenset[str],
) -> None:
    objections = _unresolved_typed_objections(positions, resolved_ids)
    safety = any(position.category == "safety" for position in objections)
    if safety and resolution not in ("experiment", "escalation"):
        raise ValueError("unresolved safety objection cannot be bypassed")
    allowed = ("experiment", "escalation", "recorded_disagreement", "scoped_authority")
    if objections and resolution not in allowed:
        raise ValueError("unresolved typed objection requires an explicit resolution mode")


def _validate_authority_resolution(
    record: DecisionRecord,
    resolution: Resolution,
    authority_holder: str | None,
) -> None:
    valid_holder = authority_holder and authority_holder in record.participants
    valid_scope = record.authority_scope and record.authority_scope in record.scopes_for(authority_holder or "")
    if resolution == "scoped_authority" and not (valid_holder and valid_scope):
        raise ValueError("scoped authority requires a participating holder with the declared scope")
