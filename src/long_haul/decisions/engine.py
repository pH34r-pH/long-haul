"""Deterministic protocol policy, intentionally separate from LLM adapters."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..models import DecisionPosition, Position

Resolution = Literal["consensus", "consent", "scoped_authority", "experiment", "escalation", "recorded_disagreement"]
class DecisionRecord(BaseModel):
    id: str; proposal: str; participants: set[str]
    consequential: bool = True; exception: str | None = None
    initial_positions: list[DecisionPosition] = Field(default_factory=list)
    final_positions: list[DecisionPosition] = Field(default_factory=list)
    resolution: Resolution | None = None; action: str | None = None; authority_holder: str | None = None
    @model_validator(mode="after")
    def validate_blocks(self) -> DecisionRecord:
        for position in self.initial_positions + self.final_positions:
            if position.position is Position.BLOCK and (not position.rationale or position.category == "preference"):
                raise ValueError("a block needs non-preference typed rationale")
        return self

class DecisionEngine:
    def capture_initial(self, record: DecisionRecord, position: DecisionPosition) -> None:
        if position.participant not in record.participants: raise ValueError("unknown participant")
        if any(p.participant == position.participant for p in record.initial_positions): raise ValueError("initial position already captured")
        record.initial_positions.append(position)
    def resolve(self, record: DecisionRecord, resolution: Resolution, action: str, authority_holder: str | None = None) -> DecisionRecord:
        if record.consequential and len(record.initial_positions) != len(record.participants) and not record.exception:
            raise ValueError("consequential decision requires independent initial positions")
        blocks = [p for p in record.initial_positions if p.position is Position.BLOCK]
        if blocks and resolution not in ("escalation", "experiment"): raise ValueError("unresolved block cannot be bypassed")
        if resolution == "scoped_authority" and not authority_holder: raise ValueError("scoped authority requires an authority holder")
        record.resolution, record.action, record.authority_holder = resolution, action, authority_holder
        return record
