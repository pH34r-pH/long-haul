"""Deterministic decision policy over immutable position history."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..events.store import Event
from ..models import DecisionPosition, Position
from .objections import Objection, ObjectionProjection, materialize_objections

Resolution = Literal["consensus", "consent", "scoped_authority", "experiment", "escalation", "recorded_disagreement"]


def _positions_for(record: DecisionRecord) -> tuple[DecisionPosition, ...]:
    return (*record.initial_positions, *record.final_positions)


def _validate_position_participants(
    positions: Sequence[DecisionPosition], participants: frozenset[str]
) -> None:
    names = [position.participant for position in positions]
    if len(names) != len(set(names)):
        raise ValueError("a participant may have only one position per phase")
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
        return record.model_copy(update={"initial_positions": (*record.initial_positions, position)})

    def capture_final(self, record: DecisionRecord, position: DecisionPosition) -> DecisionRecord:
        if record.resolution is not None:
            raise ValueError("cannot capture a final position after resolution")
        if position.participant not in record.participants:
            raise ValueError("unknown participant")
        if any(existing.participant == position.participant for existing in record.final_positions):
            raise ValueError("final position already captured")
        return record.model_copy(update={"final_positions": (*record.final_positions, position)})

    def resolve(
        self,
        record: DecisionRecord,
        resolution: Resolution,
        action: str,
        authority_holder: str | None = None,
    ) -> DecisionRecord:
        self._validate_resolution(record, resolution, authority_holder, frozenset())
        return record.model_copy(
            update={
                "resolution": resolution,
                "action": action,
                "authority_holder": authority_holder,
                "resolved_objection_ids": (),
            }
        )

    def resolve_with_events(
        self,
        record: DecisionRecord,
        resolution: Resolution,
        action: str,
        events: Sequence[Event],
        authority_holder: str | None = None,
    ) -> DecisionRecord:
        """Resolve using the active objection view rebuilt from canonical events."""
        projection = materialize_objections(events, record)
        _validate_position_objection_links(record, projection)
        active = projection.blocking_for_decision()
        _validate_active_objections(active, resolution)
        resolved = projection.terminally_disposed_ids()
        self._validate_resolution(record, resolution, authority_holder, resolved)
        return record.model_copy(
            update={
                "resolution": resolution,
                "action": action,
                "authority_holder": authority_holder,
                "resolved_objection_ids": tuple(sorted(resolved)),
            }
        )

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
    incomplete = len(record.initial_positions) != len(record.participants)
    if record.consequential and incomplete and not record.exception:
        raise ValueError("consequential decision requires independent initial positions")


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
