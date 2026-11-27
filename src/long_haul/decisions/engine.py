"""Deterministic decision protocol policy, capture/reveal boundary, and dispatch gate (#290)."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..events import Event, EventStore
from ..models import DecisionPosition, Position

Resolution = Literal["consensus", "consent", "scoped_authority", "experiment", "escalation", "recorded_disagreement"]
T = TypeVar("T")


def _check_block_rationale(position: DecisionPosition) -> None:
    if position.position is Position.BLOCK and (not position.rationale or not position.rationale.strip() or position.category in (None, "preference")):
        raise ValueError("a block needs non-preference typed rationale")


def _check_scoped_authority(record: DecisionRecord, authority_holder: str | None) -> None:
    allowed = record.scopes_for(authority_holder) if authority_holder else frozenset()
    if not authority_holder or authority_holder not in record.participants or not record.authority_scope or record.authority_scope not in allowed:
        raise ValueError("scoped authority requires a participating holder with the declared scope")


def _validate_positions_list(
    positions: Sequence[DecisionPosition],
    participants: frozenset[str],
    *,
    stage: str,
) -> list[DecisionPosition]:
    seen: set[str] = set()
    cloned: list[DecisionPosition] = []
    for position in positions:
        if position.participant not in participants:
            raise ValueError(f"unknown participant in {stage}: {position.participant!r}")
        if position.participant in seen:
            raise ValueError(f"duplicate participant in {stage}: {position.participant!r}")
        _check_block_rationale(position)
        seen.add(position.participant)
        cloned.append(position.model_copy(deep=True))
    return cloned


class DecisionRecord(BaseModel):
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

    @field_validator("authority_scopes", mode="before")
    @classmethod
    def normalize_authority_scopes(cls, value: object) -> object:
        items = value.items() if isinstance(value, Mapping) else value or ()
        return tuple(sorted((str(holder), tuple(sorted(set(scopes)))) for holder, scopes in items))

    @model_validator(mode="after")
    def validate_record_invariants(self) -> DecisionRecord:
        _validate_positions_list(self.initial_positions, self.participants, stage="initial_positions")
        _validate_positions_list(self.final_positions, self.participants, stage="final_positions")
        return self

    def scopes_for(self, participant: str) -> frozenset[str]:
        return frozenset(scope for holder, scopes in self.authority_scopes if holder == participant for scope in scopes)


def _updated(record: DecisionRecord, **changes: object) -> DecisionRecord:
    return DecisionRecord.model_validate({**record.model_dump(), **changes})


class DecisionEngine:
    """Enforce independent capture, reveal boundaries, current objection state, and provenance."""

    @staticmethod
    def _has_complete_initial_coverage(record: DecisionRecord) -> bool:
        captured = {p.participant for p in record.initial_positions}
        return captured == record.participants

    def capture_initial(self, record: DecisionRecord, position: DecisionPosition) -> DecisionRecord:
        if record.resolution is not None:
            raise ValueError("cannot capture initial position after decision resolution")
        if position.participant not in record.participants:
            raise ValueError("unknown participant")
        if any(p.participant == position.participant for p in record.initial_positions):
            raise ValueError("initial position already captured")
        _check_block_rationale(position)
        return _updated(record, initial_positions=(*record.initial_positions, position))

    def reveal_initial_positions(self, record: DecisionRecord) -> tuple[DecisionPosition, ...]:
        """Reveal peer initial positions only after full independent capture or explicit exception."""
        if record.consequential and not self._has_complete_initial_coverage(record) and not record.exception:
            raise ValueError("cannot reveal peer positions before all required initial captures complete")
        return tuple(p.model_copy(deep=True) for p in record.initial_positions)

    def record_final(self, record: DecisionRecord, position: DecisionPosition) -> DecisionRecord:
        """Record a post-deliberation disposition while preserving immutable initial capture history."""
        if record.resolution is not None:
            raise ValueError("cannot modify positions after decision resolution")
        if position.participant not in record.participants:
            raise ValueError("unknown participant")
        if record.consequential and not self._has_complete_initial_coverage(record) and not record.exception:
            raise ValueError("final disposition requires complete initial capture or recorded exception")
        _check_block_rationale(position)
        prior = self.effective_positions(record).get(position.participant)
        withdrawing_block = prior is not None and prior.position is Position.BLOCK and position.position is not Position.BLOCK
        if withdrawing_block and (not position.rationale or not position.rationale.strip()):
            raise ValueError("withdrawing or resolving a block requires an explicit rationale")
        snapshot = position.model_copy(deep=True)
        remaining = tuple(p for p in record.final_positions if p.participant != snapshot.participant)
        return _updated(record, final_positions=(*remaining, snapshot))

    @staticmethod
    def effective_positions(record: DecisionRecord) -> dict[str, DecisionPosition]:
        """Compute current active position per participant (final overrides initial)."""
        current = {p.participant: p.model_copy(deep=True) for p in record.initial_positions}
        for pos in record.final_positions:
            current[pos.participant] = pos.model_copy(deep=True)
        return current

    def active_blocks(self, record: DecisionRecord) -> list[DecisionPosition]:
        return [p for p in self.effective_positions(record).values() if p.position is Position.BLOCK]

    def resolve(
        self,
        record: DecisionRecord,
        resolution: Resolution,
        action: str,
        authority_holder: str | None = None,
        *,
        event_store: EventStore | None = None,
    ) -> DecisionRecord:
        if record.resolution is not None:
            raise ValueError("decision is already resolved")
        if not action or not action.strip():
            raise ValueError("decision resolution requires a nonempty action")
        _validate_positions_list(record.initial_positions, record.participants, stage="initial_positions")
        _validate_positions_list(record.final_positions, record.participants, stage="final_positions")
        if record.consequential and not self._has_complete_initial_coverage(record) and not record.exception:
            raise ValueError("consequential decision requires independent initial positions from every participant")
        if self.active_blocks(record) and resolution not in ("escalation", "experiment"):
            raise ValueError("unresolved block cannot be bypassed")
        if resolution == "scoped_authority":
            _check_scoped_authority(record, authority_holder)
        resolved = _updated(record, resolution=resolution, action=action, authority_holder=authority_holder)
        if event_store is not None:
            event_store.append(
                Event(
                    event_type="decision",
                    actor=authority_holder,
                    source="decision_engine",
                    payload={"record": resolved.model_dump(mode="json")},
                )
            )
        return resolved

    def dispatch_consequential(
        self,
        record: DecisionRecord,
        executor: Callable[[DecisionRecord], T],
    ) -> T:
        """Refuse consequential dispatch until an authoritative lifecycle is integrated.

        A mutable record, even one with a resolution field, does not prove
        independent capture or authorized, replayable objection transitions.
        This helper is not a runtime authorization boundary.
        """
        raise ValueError("consequential decision dispatch is not integrated; refusing execution")


def replay_decisions(events: list[Event]) -> dict[str, DecisionRecord]:
    """Reconstruct resolved DecisionRecord instances from authoritative decision events."""
    decisions: dict[str, DecisionRecord] = {}
    for event in events:
        if event.event_type != "decision":
            continue
        raw = event.payload.get("record")
        if isinstance(raw, dict):
            record = DecisionRecord.model_validate(raw)
            decisions[record.id] = record
    return decisions
