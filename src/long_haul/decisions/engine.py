"""Deterministic decision policy over immutable position history."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..events.store import Event
from ..models import DecisionPosition, Position
from .objections import materialize_objections

Resolution = Literal["consensus", "consent", "scoped_authority", "experiment", "escalation", "recorded_disagreement"]


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
            participants = [position.participant for position in positions]
            if len(participants) != len(set(participants)):
                raise ValueError("a participant may have only one position per phase")
            if any(participant not in self.participants for participant in participants):
                raise ValueError("position participant is not part of the decision")
        unresolved_blocks = [
            position
            for position in (*self.initial_positions, *self.final_positions)
            if position.position is Position.BLOCK and position.objection_id not in self.resolved_objection_ids
        ]
        if unresolved_blocks and self.resolution not in (None, "experiment", "escalation"):
            raise ValueError("an unresolved initial block cannot be bypassed")
        objections = [
            position
            for position in (*self.initial_positions, *self.final_positions)
            if position.position is Position.OBJECT
            and position.category in ("factual", "safety", "resource")
            and position.objection_id not in self.resolved_objection_ids
        ]
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
        for position in (*record.initial_positions, *record.final_positions):
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
                (snapshot for snapshot in objection.applicability_history
                 if snapshot.revision == position.applicability_revision),
                None,
            )
            if applicability is None:
                raise ValueError("position applicability revision is absent from objection history")
            if applicability.scope != position.applicability_scope:
                raise ValueError("position applicability scope differs from its linked objection history")
        active = projection.blocking_for_decision()
        if active and resolution not in ("experiment", "escalation", "recorded_disagreement", "scoped_authority"):
            raise ValueError("active objection cannot be bypassed")
        if any(objection.category == "safety" for objection in active) and resolution not in ("experiment", "escalation"):
            raise ValueError("active objection with safety category cannot be bypassed")
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
        if record.consequential and len(record.initial_positions) != len(record.participants) and not record.exception:
            raise ValueError("consequential decision requires independent initial positions")
        blocks = [
            position
            for position in (*record.initial_positions, *record.final_positions)
            if position.position is Position.BLOCK and position.objection_id not in resolved_objection_ids
        ]
        if blocks and resolution not in ("escalation", "experiment"):
            raise ValueError("unresolved block cannot be bypassed")
        unresolved_typed_objections = [
            position
            for position in (*record.initial_positions, *record.final_positions)
            if position.position is Position.OBJECT
            and position.category in ("factual", "safety", "resource")
            and position.objection_id not in resolved_objection_ids
        ]
        unresolved_safety_objections = [
            position for position in unresolved_typed_objections if position.category == "safety"
        ]
        if unresolved_safety_objections and resolution not in ("experiment", "escalation"):
            raise ValueError("unresolved safety objection cannot be bypassed")
        if unresolved_typed_objections and resolution not in (
            "experiment", "escalation", "recorded_disagreement", "scoped_authority"
        ):
            raise ValueError("unresolved typed objection requires an explicit resolution mode")
        if resolution == "scoped_authority" and (
            not authority_holder
            or authority_holder not in record.participants
            or not record.authority_scope
            or record.authority_scope not in record.scopes_for(authority_holder)
        ):
            raise ValueError("scoped authority requires a participating holder with the declared scope")
