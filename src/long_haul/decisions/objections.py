"""Append-only objection lifecycle over the existing Long Haul event store."""
from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from time import perf_counter_ns
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..events.store import Event, EventStore

if TYPE_CHECKING:
    from .engine import DecisionRecord

ObjectionStatus = Literal["active", "narrowed", "dismissed", "resolved", "superseded"]
Operation = Literal["raised", "narrowed", "assessed", "resolved", "dismissed", "superseded", "reopened"]
AssessmentOutcome = Literal["satisfied", "not_satisfied", "unknown"]
ObjectionCategory = Literal["factual", "safety", "resource", "value", "jurisdiction", "preference", "other"]


class EvidenceAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_ref: str
    predicate_id: str
    outcome: AssessmentOutcome
    actor: str
    source: str


class ApplicabilitySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    revision: int = Field(ge=1)
    scope: str
    resolution_predicate_id: str
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple)


class Objection(BaseModel):
    """Current rebuildable view of one objection; the event log stays authoritative."""

    model_config = ConfigDict(frozen=True)

    id: str
    decision_id: str
    raised_by: str
    category: ObjectionCategory
    claim: str
    scope: str
    authority_scope: str
    resolution_predicate_id: str
    applicability_revision: int = Field(ge=1)
    applicability_history: tuple[ApplicabilitySnapshot, ...]
    revision: int = Field(ge=1)
    status: ObjectionStatus = "active"
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple)
    assessments: tuple[EvidenceAssessment, ...] = Field(default_factory=tuple)
    reason: str | None = None
    superseded_by: str | None = None

    @model_validator(mode="after")
    def valid_applicability_history(self) -> Objection:
        revisions = tuple(snapshot.revision for snapshot in self.applicability_history)
        if revisions != tuple(range(1, self.applicability_revision + 1)):
            raise ValueError("objection applicability history must be contiguous")
        latest = self.applicability_history[-1]
        if latest.scope != self.scope or latest.resolution_predicate_id != self.resolution_predicate_id:
            raise ValueError("current objection applicability differs from its history")
        return self

    @property
    def is_active(self) -> bool:
        return self.status in ("active", "narrowed")

    @property
    def blocks_ordinary_resolution(self) -> bool:
        return self.is_active and self.category != "preference"


class ObjectionProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: str
    objections: tuple[Objection, ...] = Field(default_factory=tuple)
    metrics: ObjectionProjectionMetrics

    def active_for_decision(self, include_live_view: bool = True) -> tuple[Objection, ...]:
        """Return the special structured view, or hide it for a matched baseline."""
        if not include_live_view:
            return ()
        return tuple(objection for objection in self.objections if objection.is_active)

    def blocking_for_decision(self) -> tuple[Objection, ...]:
        """Return current objections that constrain ordinary resolution."""
        return tuple(objection for objection in self.objections if objection.blocks_ordinary_resolution)

    def terminally_disposed_ids(self) -> frozenset[str]:
        """Return terminal IDs, propagating disposition through supersession chains."""
        by_id = {objection.id: objection for objection in self.objections}
        terminal: set[str] = set()
        for objection in self.objections:
            path: list[str] = []
            visited: set[str] = set()
            current = objection
            while current.status == "superseded":
                if current.id in visited or current.superseded_by not in by_id:
                    raise ValueError("supersession history contains a cycle or missing successor")
                visited.add(current.id)
                path.append(current.id)
                current = by_id[current.superseded_by]
            if current.status in ("resolved", "dismissed"):
                terminal.update((*path, current.id))
        return frozenset(terminal)

    def get(self, objection_id: str) -> Objection:
        for objection in self.objections:
            if objection.id == objection_id:
                return objection
        raise KeyError(objection_id)


class ObjectionProjectionMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    events_scanned: int = Field(ge=0)
    lifecycle_events: int = Field(ge=0)
    serialized_event_bytes: int = Field(ge=0)
    view_bytes: int = Field(ge=0)
    active_count: int = Field(ge=0)
    replay_elapsed_ns: int = Field(ge=0)


class ObjectionTransition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    operation: Operation
    decision_id: str
    objection_id: str
    expected_revision: int | None = None
    category: ObjectionCategory | None = None
    claim: str | None = None
    scope: str | None = None
    authority_scope: str | None = None
    resolution_predicate_id: str | None = None
    applicability_revision: int | None = None
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple)
    evidence_ref: str | None = None
    predicate_id: str | None = None
    assessment: AssessmentOutcome | None = None
    reason: str | None = None
    successor_id: str | None = None

    @model_validator(mode="after")
    def operation_fields(self) -> ObjectionTransition:
        if self.operation == "raised":
            if self.expected_revision is not None or not all(
                (self.category, self.claim, self.scope, self.authority_scope, self.resolution_predicate_id)
            ):
                raise ValueError("raising requires a category, claim, scope, authority scope, and resolution predicate")
            if self.applicability_revision not in (None, 1):
                raise ValueError("a new objection starts at applicability revision one")
        else:
            if self.expected_revision is None or self.expected_revision < 1:
                raise ValueError("transition requires an expected objection revision")
        if self.operation == "narrowed" and not all((self.scope, self.reason, self.evidence_refs)):
            raise ValueError("narrowing requires a new scope, reason, and evidence reference")
        if self.operation == "assessed" and not all((self.evidence_ref, self.predicate_id, self.assessment)):
            raise ValueError("evidence assessment requires a reference, predicate, and outcome")
        if self.operation in ("resolved", "dismissed") and not self.reason:
            raise ValueError("explicit disposition requires a reason")
        if self.operation == "superseded" and not all((self.successor_id, self.reason)):
            raise ValueError("superseding requires an existing successor and reason")
        if self.operation == "reopened" and not all(
            (self.scope, self.resolution_predicate_id, self.applicability_revision, self.evidence_ref, self.reason)
        ):
            raise ValueError("reopening requires changed applicability, new evidence, and reason")
        return self


def materialize_objections(events: Sequence[Event], decision: DecisionRecord) -> ObjectionProjection:
    """Replay objection events, rejecting conflicting duplicates and stale transitions."""
    started_ns = perf_counter_ns()
    seen: dict[str, str] = {}
    objections: dict[str, Objection] = {}
    lifecycle_events = 0
    serialized_event_bytes = 0
    events_scanned = 0
    for event in events:
        serialized = event.model_dump_json()
        events_scanned += 1
        serialized_event_bytes += len(serialized.encode("utf-8"))
        previous = seen.get(event.id)
        if previous is not None:
            if previous != serialized:
                raise ValueError("conflicting duplicate event identity")
            continue
        seen[event.id] = serialized
        if event.event_type != "objection_lifecycle":
            continue
        lifecycle_events += 1
        transition = ObjectionTransition.model_validate(event.payload)
        if transition.decision_id != decision.id:
            continue
        if not event.actor or event.actor not in decision.participants:
            raise ValueError("objection event actor must be a decision participant")
        if not event.source:
            raise ValueError("objection event needs provenance source")
        _apply_transition(objections, transition, event.actor, event.source, decision)
    objection_view = tuple(objections[key] for key in sorted(objections))
    active_count = sum(objection.is_active for objection in objection_view)
    view_bytes = len(
        json.dumps(
            {
                "decision_id": decision.id,
                "objections": [objection.model_dump(mode="json") for objection in objection_view],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    metrics = ObjectionProjectionMetrics(
        events_scanned=events_scanned,
        lifecycle_events=lifecycle_events,
        serialized_event_bytes=serialized_event_bytes,
        view_bytes=view_bytes,
        active_count=active_count,
        replay_elapsed_ns=perf_counter_ns() - started_ns,
    )
    return ObjectionProjection(decision_id=decision.id, objections=objection_view, metrics=metrics)


class ObjectionLifecycle:
    """Typed commands that append to an existing EventStore and validate by replay."""

    def __init__(self, store: EventStore, decision: DecisionRecord):
        self.store = store
        self.decision = decision
        self.last_update_elapsed_ns: int | None = None

    @property
    def projection(self) -> ObjectionProjection:
        return materialize_objections(self.store.events(), self.decision)

    def raise_objection(
        self,
        *,
        objection_id: str,
        actor: str,
        source: str,
        category: ObjectionCategory,
        claim: str,
        scope: str,
        authority_scope: str,
        resolution_predicate_id: str,
        evidence_refs: Iterable[str] = (),
    ) -> ObjectionProjection:
        return self._append(
            ObjectionTransition(
                operation="raised",
                decision_id=self.decision.id,
                objection_id=objection_id,
                category=category,
                claim=claim,
                scope=scope,
                authority_scope=authority_scope,
                resolution_predicate_id=resolution_predicate_id,
                applicability_revision=1,
                evidence_refs=tuple(evidence_refs),
            ),
            actor,
            source,
        )

    def narrow(
        self,
        objection_id: str,
        *,
        actor: str,
        source: str,
        expected_revision: int,
        scope: str,
        reason: str,
        evidence_refs: Iterable[str],
    ) -> ObjectionProjection:
        return self._append(
            ObjectionTransition(
                operation="narrowed",
                decision_id=self.decision.id,
                objection_id=objection_id,
                expected_revision=expected_revision,
                scope=scope,
                applicability_revision=self._next_applicability_revision(objection_id, expected_revision),
                reason=reason,
                evidence_refs=tuple(evidence_refs),
            ),
            actor,
            source,
        )

    def assess_evidence(
        self,
        objection_id: str,
        *,
        actor: str,
        source: str,
        expected_revision: int,
        evidence_ref: str,
        predicate_id: str,
        outcome: AssessmentOutcome,
    ) -> ObjectionProjection:
        return self._append(
            ObjectionTransition(
                operation="assessed",
                decision_id=self.decision.id,
                objection_id=objection_id,
                expected_revision=expected_revision,
                evidence_ref=evidence_ref,
                predicate_id=predicate_id,
                assessment=outcome,
            ),
            actor,
            source,
        )

    def resolve(
        self,
        objection_id: str,
        *,
        actor: str,
        source: str,
        expected_revision: int,
        reason: str,
        evidence_refs: Iterable[str] = (),
    ) -> ObjectionProjection:
        return self._append(
            ObjectionTransition(
                operation="resolved",
                decision_id=self.decision.id,
                objection_id=objection_id,
                expected_revision=expected_revision,
                reason=reason,
                evidence_refs=tuple(evidence_refs),
            ),
            actor,
            source,
        )

    def dismiss(
        self,
        objection_id: str,
        *,
        actor: str,
        source: str,
        expected_revision: int,
        reason: str,
        evidence_refs: Iterable[str] = (),
    ) -> ObjectionProjection:
        return self._append(
            ObjectionTransition(
                operation="dismissed",
                decision_id=self.decision.id,
                objection_id=objection_id,
                expected_revision=expected_revision,
                reason=reason,
                evidence_refs=tuple(evidence_refs),
            ),
            actor,
            source,
        )

    def supersede(
        self,
        objection_id: str,
        *,
        actor: str,
        source: str,
        expected_revision: int,
        successor_id: str,
        reason: str,
    ) -> ObjectionProjection:
        return self._append(
            ObjectionTransition(
                operation="superseded",
                decision_id=self.decision.id,
                objection_id=objection_id,
                expected_revision=expected_revision,
                successor_id=successor_id,
                reason=reason,
            ),
            actor,
            source,
        )

    def reopen(
        self,
        objection_id: str,
        *,
        actor: str,
        source: str,
        expected_revision: int,
        scope: str,
        resolution_predicate_id: str,
        evidence_ref: str,
        reason: str,
    ) -> ObjectionProjection:
        return self._append(
            ObjectionTransition(
                operation="reopened",
                decision_id=self.decision.id,
                objection_id=objection_id,
                expected_revision=expected_revision,
                scope=scope,
                resolution_predicate_id=resolution_predicate_id,
                applicability_revision=self._next_applicability_revision(objection_id, expected_revision),
                evidence_ref=evidence_ref,
                reason=reason,
            ),
            actor,
            source,
        )

    def _append(self, transition: ObjectionTransition, actor: str, source: str) -> ObjectionProjection:
        started_ns = perf_counter_ns()
        event = Event(
            event_type="objection_lifecycle",
            actor=actor,
            source=source,
            payload=transition.model_dump(mode="json"),
        )
        replayed = materialize_objections((*self.store.events(), event), self.decision)
        self.store.append(event)
        self.last_update_elapsed_ns = perf_counter_ns() - started_ns
        return replayed

    def _next_applicability_revision(self, objection_id: str, expected_revision: int) -> int:
        objection = self.projection.get(objection_id)
        if objection.revision != expected_revision:
            raise ValueError("stale objection revision")
        return objection.applicability_revision + 1


def _apply_transition(
    objections: dict[str, Objection],
    transition: ObjectionTransition,
    actor: str,
    source: str,
    decision: DecisionRecord,
) -> None:
    if transition.operation == "raised":
        if transition.objection_id in objections:
            raise ValueError("objection identity already exists")
        assert transition.category and transition.claim and transition.scope and transition.authority_scope
        assert transition.resolution_predicate_id
        if not any(
            holder in decision.participants and transition.authority_scope in scopes
            for holder, scopes in decision.authority_scopes
        ):
            raise ValueError("objection authority scope has no participating resolver")
        objections[transition.objection_id] = Objection(
            id=transition.objection_id,
            decision_id=decision.id,
            raised_by=actor,
            category=transition.category,
            claim=transition.claim,
            scope=transition.scope,
            authority_scope=transition.authority_scope,
            resolution_predicate_id=transition.resolution_predicate_id,
            applicability_revision=1,
            applicability_history=(
                ApplicabilitySnapshot(
                    revision=1,
                    scope=transition.scope,
                    resolution_predicate_id=transition.resolution_predicate_id,
                    evidence_refs=transition.evidence_refs,
                ),
            ),
            revision=1,
            evidence_refs=transition.evidence_refs,
        )
        return

    objection = objections.get(transition.objection_id)
    if objection is None:
        raise ValueError("objection transition precedes its raise event")
    if transition.expected_revision != objection.revision:
        raise ValueError("stale or out-of-order objection transition")
    if transition.operation == "narrowed":
        if not objection.is_active:
            raise ValueError("terminal objection must be reopened before narrowing")
        if actor != objection.raised_by and not _has_authority(decision, actor, objection.authority_scope):
            raise ValueError("only the raiser or scoped authority may narrow an objection")
        assert transition.scope and transition.reason and transition.applicability_revision
        if transition.applicability_revision != objection.applicability_revision + 1:
            raise ValueError("narrowing needs the next applicability revision")
        evidence_refs = _append_unique(objection.evidence_refs, transition.evidence_refs)
        applicability_history = (
            *objection.applicability_history,
            ApplicabilitySnapshot(
                revision=transition.applicability_revision,
                scope=transition.scope,
                resolution_predicate_id=objection.resolution_predicate_id,
                evidence_refs=evidence_refs,
            ),
        )
        objections[objection.id] = objection.model_copy(
            update={
                "scope": transition.scope,
                "applicability_revision": transition.applicability_revision,
                "applicability_history": applicability_history,
                "revision": objection.revision + 1,
                "status": "narrowed",
                "evidence_refs": evidence_refs,
                "reason": transition.reason,
            }
        )
        return

    if transition.operation == "assessed":
        if not objection.is_active:
            raise ValueError("terminal objection must be reopened before evidence assessment")
        assert transition.evidence_ref and transition.predicate_id and transition.assessment
        assessment = EvidenceAssessment(
            evidence_ref=transition.evidence_ref,
            predicate_id=transition.predicate_id,
            outcome=transition.assessment,
            actor=actor,
            source=source,
        )
        resolves = (
            transition.predicate_id == objection.resolution_predicate_id
            and transition.assessment == "satisfied"
            and _has_authority(decision, actor, objection.authority_scope)
        )
        objections[objection.id] = objection.model_copy(
            update={
                "revision": objection.revision + 1,
                "status": "resolved" if resolves else objection.status,
                "evidence_refs": _append_unique(objection.evidence_refs, (transition.evidence_ref,)),
                "assessments": (*objection.assessments, assessment),
                "reason": "declared resolution predicate satisfied" if resolves else objection.reason,
            }
        )
        return

    if not _has_authority(decision, actor, objection.authority_scope):
        raise ValueError("objection disposition requires declared scoped authority")
    if transition.operation in ("resolved", "dismissed"):
        if not objection.is_active:
            raise ValueError("terminal objection cannot receive another terminal disposition")
        assert transition.reason
        objections[objection.id] = objection.model_copy(
            update={
                "revision": objection.revision + 1,
                "status": "resolved" if transition.operation == "resolved" else "dismissed",
                "evidence_refs": _append_unique(objection.evidence_refs, transition.evidence_refs),
                "reason": transition.reason,
            }
        )
        return

    if transition.operation == "superseded":
        successor = objections.get(transition.successor_id or "")
        if transition.successor_id == objection.id:
            raise ValueError("an objection cannot supersede itself")
        if not objection.is_active or not successor or not successor.is_active:
            raise ValueError("superseding requires an active successor and active prior objection")
        if (successor.category, successor.authority_scope) != (objection.category, objection.authority_scope):
            raise ValueError("successor must preserve objection category and resolver authority scope")
        assert transition.reason
        objections[objection.id] = objection.model_copy(
            update={"revision": objection.revision + 1, "status": "superseded", "reason": transition.reason, "superseded_by": successor.id}
        )
        return

    if transition.operation == "reopened":
        if objection.status not in ("dismissed", "resolved"):
            raise ValueError("only a dismissed or resolved objection can be reopened")
        assert transition.scope and transition.resolution_predicate_id
        assert transition.applicability_revision and transition.evidence_ref and transition.reason
        if transition.applicability_revision != objection.applicability_revision + 1:
            raise ValueError("reopening needs the next applicability revision")
        evidence_refs = _append_unique(objection.evidence_refs, (transition.evidence_ref,))
        applicability_history = (
            *objection.applicability_history,
            ApplicabilitySnapshot(
                revision=transition.applicability_revision,
                scope=transition.scope,
                resolution_predicate_id=transition.resolution_predicate_id,
                evidence_refs=evidence_refs,
            ),
        )
        objections[objection.id] = objection.model_copy(
            update={
                "revision": objection.revision + 1,
                "applicability_revision": transition.applicability_revision,
                "applicability_history": applicability_history,
                "scope": transition.scope,
                "resolution_predicate_id": transition.resolution_predicate_id,
                "status": "active",
                "evidence_refs": evidence_refs,
                "reason": transition.reason,
                "superseded_by": None,
            }
        )
        return

    raise ValueError("unsupported objection operation")


def _has_authority(decision: DecisionRecord, actor: str, scope: str) -> bool:
    return actor in decision.participants and scope in decision.scopes_for(actor)


def _append_unique(existing: tuple[str, ...], additions: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*existing, *additions)))
