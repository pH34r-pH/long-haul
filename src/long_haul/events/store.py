"""Small append-only JSONL event store; SQLite is intentionally not required for MVP."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from ..models import CrewMember, Embodiment

EventType = Literal["observation", "self_report", "inference", "unknown", "request", "decision", "embodiment_change", "competence_update", "rupture", "repair"]
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

def materialize(events: list[Event], initial_crew: list[CrewMember] | None = None) -> FleetState:
    initial_crew = initial_crew or []
    state = FleetState(crew={member.crew_id: member.model_copy(deep=True) for member in initial_crew})
    for event in events:
        if event.event_type == "embodiment_change":
            member = state.crew[event.payload["crew_id"]]
            member.embodiment = Embodiment.model_validate(event.payload["embodiment"])
            member.generation = event.payload.get("generation", member.generation + 1)
        elif event.event_type == "competence_update":
            state.crew[event.payload["crew_id"]].competence = event.payload["competence"]
        elif event.event_type == "rupture": state.ruptures.append(event.payload)
        elif event.event_type == "repair": state.ruptures = [r for r in state.ruptures if r.get("id") != event.payload.get("rupture_id")]
    return state
