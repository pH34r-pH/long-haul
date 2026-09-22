"""Durable, event-derived checkpoints for bounded work sessions."""
from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from ..events.store import Event

CHECKPOINT_SCHEMA_VERSION = 1


class CheckpointFact(BaseModel):
    text: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)


class ArtifactState(BaseModel):
    fingerprint: str = Field(min_length=1)
    description: str = ""
    source_event_id: str = Field(min_length=1)


class WorkCheckpoint(BaseModel):
    contract_id: str = Field(min_length=1)
    facts: list[CheckpointFact] = Field(default_factory=list)
    active_constraints: list[str] = Field(default_factory=list)
    completed_subgoals: list[str] = Field(default_factory=list)
    remaining_subgoals: list[str] = Field(default_factory=list)
    unresolved_failures: list[str] = Field(default_factory=list)
    artifact: ArtifactState | None = None
    through_event_id: str | None = None
    schema_version: Literal[1] = CHECKPOINT_SCHEMA_VERSION


class TaskDigest(BaseModel):
    contract_id: str
    text: str
    max_chars: int = Field(ge=1)
    truncated: bool = False
    omitted_items: int = Field(default=0, ge=0)


class WorkSession(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    contract_id: str
    crew_id: str | None = None
    execution_plan_id: str | None = None
    resumed_from_session_id: str | None = None
    checkpoint_through_event_id: str | None = None


def resume_session(previous: WorkSession, checkpoint: WorkCheckpoint) -> WorkSession:
    if previous.contract_id != checkpoint.contract_id:
        raise ValueError("checkpoint contract does not match session contract")
    return WorkSession(
        contract_id=previous.contract_id,
        crew_id=previous.crew_id,
        execution_plan_id=previous.execution_plan_id,
        resumed_from_session_id=previous.session_id,
        checkpoint_through_event_id=checkpoint.through_event_id,
    )


def materialize_checkpoint(contract_id: str, events: list[Event]) -> WorkCheckpoint:
    """Materialize compact state from authoritative events in append order."""
    checkpoint = WorkCheckpoint(contract_id=contract_id)
    fact_keys: set[str] = set()

    def belongs(event: Event) -> bool:
        return event.payload.get("contract_id") == contract_id

    for event in events:
        if not belongs(event):
            continue
        payload = event.payload
        checkpoint.through_event_id = event.id
        if event.event_type == "work_fact":
            text = str(payload.get("text", "")).strip()
            if text and text not in fact_keys:
                checkpoint.facts.append(CheckpointFact(text=text, source_event_id=event.id))
                fact_keys.add(text)
        elif event.event_type == "work_constraint":
            text = str(payload.get("text", "")).strip()
            if text and text not in checkpoint.active_constraints:
                checkpoint.active_constraints.append(text)
        elif event.event_type == "work_subgoal":
            text = str(payload.get("text", "")).strip()
            state = payload.get("state")
            if not text:
                continue
            if state == "completed":
                if text not in checkpoint.completed_subgoals:
                    checkpoint.completed_subgoals.append(text)
                checkpoint.remaining_subgoals = [item for item in checkpoint.remaining_subgoals if item != text]
            elif state == "remaining" and text not in checkpoint.completed_subgoals and text not in checkpoint.remaining_subgoals:
                checkpoint.remaining_subgoals.append(text)
        elif event.event_type == "work_failure":
            failure_id = str(payload.get("failure_id", event.id))
            detail = str(payload.get("detail", failure_id)).strip()
            checkpoint.unresolved_failures = [item for item in checkpoint.unresolved_failures if not item.startswith(f"{failure_id}:")]
            checkpoint.unresolved_failures.append(f"{failure_id}: {detail}")
        elif event.event_type == "work_failure_resolved":
            failure_id = str(payload.get("failure_id", ""))
            checkpoint.unresolved_failures = [item for item in checkpoint.unresolved_failures if not item.startswith(f"{failure_id}:")]
        elif event.event_type == "work_artifact_verified":
            fingerprint = str(payload.get("fingerprint", "")).strip()
            if fingerprint:
                checkpoint.artifact = ArtifactState(
                    fingerprint=fingerprint,
                    description=str(payload.get("description", "")),
                    source_event_id=event.id,
                )
    return checkpoint


def render_digest(checkpoint: WorkCheckpoint, max_chars: int = 4000) -> TaskDigest:
    """Render newest useful state first, dropping whole items to fit a hard bound."""
    header = f"Work contract: {checkpoint.contract_id}"
    sections: list[tuple[str, list[str]]] = [
        ("Verified artifact", [f"{checkpoint.artifact.fingerprint} — {checkpoint.artifact.description}".rstrip(" —")] if checkpoint.artifact else []),
        ("Active constraints", checkpoint.active_constraints),
        ("Unresolved failures", checkpoint.unresolved_failures),
        ("Remaining subgoals", checkpoint.remaining_subgoals),
        ("Completed subgoals", checkpoint.completed_subgoals),
        ("Facts", [fact.text for fact in checkpoint.facts]),
    ]
    lines = [header]
    omitted = 0
    for title, items in sections:
        if not items:
            continue
        title_added = False
        for item in reversed(items):
            prefix = f"{title}:\n" if not title_added else ""
            candidate = "\n".join([*lines, f"{prefix}- {item}"])
            if len(candidate) <= max_chars:
                if not title_added:
                    lines.append(f"{title}:")
                    title_added = True
                lines.append(f"- {item}")
            else:
                omitted += 1
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars]
    return TaskDigest(contract_id=checkpoint.contract_id, text=text, max_chars=max_chars, truncated=omitted > 0, omitted_items=omitted)
