"""Harness-neutral execution records; execution is never candidate acceptance."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from io import BytesIO
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, ValidationError, field_validator, model_validator

from ..events.store import Event
from .checkpoints import TaskDigest, WorkCheckpoint, WorkSession, resume_session
from .contracts import WorkContract
from .progress import AttemptStep, ProgressKind, ProgressMonitor, StepOutcome
from .repository_models import DigestReference, FrozenProjection, content_digest

HarnessTerminal = Literal[
    "settled", "cancelled", "provider_failure", "context_exhausted", "harness_crash",
    "protocol_error", "resource_exhausted", "policy_denied", "timed_out",
]
SEMANTIC_TRACE_REFERENCE = "artifact:semantic-trace-v1"


def harness_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("harness time requires an authenticated timezone-aware clock")
    return value.astimezone(UTC)


class HarnessIdentity(FrozenProjection):
    harness_id: Literal["pi", "fixture"]
    package_name: str = Field(min_length=1, max_length=128)
    package_version: str = Field(min_length=1, max_length=64)
    source: DigestReference
    artifact: DigestReference
    configuration: DigestReference
    protocol: str = Field(min_length=1, max_length=64)


class HarnessAttempt(FrozenProjection):
    attempt_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    attempt_number: int = Field(default=1, ge=1)
    contract_id: str = Field(min_length=1, max_length=256)
    mission_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    crew_id: str = Field(min_length=1, max_length=256)
    repository_id: str = Field(min_length=1, max_length=256)
    contract_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    repository_extension_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    context_manifest: DigestReference
    harness: HarnessIdentity
    previous_attempt: DigestReference | None = None
    checkpoint: DigestReference | None = None
    digest: DigestReference | None = None


def harness_attempt(contract: WorkContract, session: WorkSession, identity: HarnessIdentity,
                    attempt_id: str, digest: TaskDigest | None = None, *, attempt_number: int = 1) -> HarnessAttempt:
    extension = contract.repository_work
    if extension is None or session.contract_id != contract.contract_id or not session.crew_id:
        raise ValueError("repository harness requires an exact contract and authenticated crew/session")
    if attempt_number > contract.budget.max_attempts:
        raise ValueError("work contract retry budget is exhausted")
    if digest is not None and (digest.contract_id != contract.contract_id or digest.context_manifest is None
                              or digest.context_manifest.digest != extension.context_manifest.digest):
        raise ValueError("harness digest belongs to a different contract or context")
    return HarnessAttempt(
        attempt_id=attempt_id, attempt_number=attempt_number,
        contract_id=contract.contract_id, mission_id=contract.mission_id,
        session_id=session.session_id, crew_id=session.crew_id, repository_id=extension.source.repository_id,
        contract_digest=content_digest(contract.model_dump_json().encode()),
        repository_extension_digest=extension.fingerprint, context_manifest=extension.context_manifest,
        harness=identity,
        digest=DigestReference(reference="artifact:task-digest", digest=content_digest(digest.model_dump_json().encode()))
        if digest else None,
    )


class NormalizedArguments(FrozenProjection):
    argument_fingerprint: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    target_fingerprint: str | None = Field(default=None, pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    path: str | None = Field(default=None, max_length=1024)
    offset: int | None = Field(default=None, ge=1)
    limit: int | None = Field(default=None, ge=1)
    query_fingerprint: str | None = Field(default=None, pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    content_fingerprint: str | None = Field(default=None, pattern=r"^hmac-sha256:[0-9a-f]{64}$")


class SemanticActionEventV1(FrozenProjection):
    schema_version: Literal[1] = 1
    attempt_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    contract_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    native_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,128}$")
    native_id_mode: Literal["keyed", "raw", "none"] = "keyed"
    sequence: int = Field(ge=1)
    wire_sequence: int = Field(ge=1)
    phase: Literal["start", "end", "lifecycle"]
    tool: Literal["read", "grep", "find", "ls", "edit", "write", "unknown"]
    action: Literal["read", "search", "edit", "write", "unknown", "recovery"]
    timestamp: datetime
    duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    outcome: StepOutcome
    progress: ProgressKind
    arguments: NormalizedArguments
    result_fingerprint: str | None = Field(default=None, pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    summary: Literal["started", "completed", "failed", "interrupted", "unknown_tool",
                     "retry_started", "retry_completed", "compaction_started", "compaction_failed", "settled",
                     "malformed_record"]
    consequence: Literal["read_only", "candidate_mutation", "unknown"]
    policy_receipt: DigestReference
    normalization: DigestReference
    execution_admission: DigestReference | None = None
    harness_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    candidate_before: DigestReference | None = None
    candidate_after: DigestReference | None = None
    _time = field_validator("timestamp")(harness_time)

    @model_validator(mode="after")
    def native_lifecycle(self):
        if self.phase == "lifecycle":
            if self.native_id is not None or self.native_id_mode != "none":
                raise ValueError("control lifecycles have no native call identifier")
        elif self.native_id is None or self.native_id_mode == "none":
            raise ValueError("tool lifecycles require an admitted native correlation identifier")
        elif self.native_id_mode == "keyed" and not re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", self.native_id):
            raise ValueError("keyed native correlation identifiers require an HMAC fingerprint")
        return self

    def attempt_step(self) -> AttemptStep:
        if self.phase != "end":
            raise ValueError("only ended actions enter progress monitoring")
        return AttemptStep(
            step_id=f"{self.attempt_id}:{self.native_id}", action=self.action,
            arguments=self.arguments.model_dump(mode="json", exclude_none=True),
            outcome=self.outcome, progress=self.progress, result_fingerprint=self.result_fingerprint,
        )

    def event(self) -> Event:
        return Event(
            id=f"{self.attempt_id}:semantic:{self.sequence}", timestamp=self.timestamp,
            event_type="observation", source="harness-semantic-action-v1",
            payload={"contract_id": self.contract_id, "semantic_action": self.model_dump(mode="json")},
        )


def progress_event(monitor: ProgressMonitor, event: SemanticActionEventV1) -> Event | None:
    intervention = monitor.observe(event.attempt_step())
    if intervention is None:
        return None
    return Event(
        timestamp=event.timestamp, event_type="work_progress_intervention", source="harness-semantic-action-v1",
        payload=monitor.recovery_event_payload(event.contract_id, event.attempt_id, intervention),
    )


class HarnessCleanup(FrozenProjection):
    attempt_id: str
    session_id: str
    boundary: Literal["fixture", "sandbox"]
    process_tree_stopped: bool = Field(strict=True)
    evidence: DigestReference


class HarnessInputEvidence(FrozenProjection):
    attempt_id: str
    session_id: str
    contract_digest: str
    context_manifest: DigestReference
    task_digest: DigestReference | None = None
    template: DigestReference | None = None
    prompt_fingerprint: str | None = Field(default=None, pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    status: Literal["requested", "acknowledged", "denied"]
    timestamp: datetime
    request: DigestReference | None = None
    policy_receipt: DigestReference
    source_receipts: tuple[DigestReference, ...] = ()
    _time = field_validator("timestamp")(harness_time)

    def event(self):
        return Event(id=self.attempt_id + ":input:" + self.status, timestamp=self.timestamp,
                     event_type="observation", source="harness-input-v1",
                     payload={"contract_digest": self.contract_digest, "harness_input": self.model_dump(mode="json")})

    def follows_request(self, prior):
        return (prior is not None and self.request is not None and self.request.digest == prior.fingerprint
                and prior.status == "requested" and self.prompt_fingerprint == prior.prompt_fingerprint
                and self.template == prior.template and self.timestamp >= prior.timestamp)


def _check_cleanup(attempt: HarnessAttempt, cleanup: HarnessCleanup) -> None:
    expected_boundary = "fixture" if attempt.harness.harness_id == "fixture" else "sandbox"
    if (cleanup.process_tree_stopped is not True or cleanup.attempt_id != attempt.attempt_id
            or cleanup.session_id != attempt.session_id or cleanup.boundary != expected_boundary):
        raise ValueError("exact trusted process-tree cleanup is required before freeze or release")


class HarnessResult(FrozenProjection):
    attempt: HarnessAttempt
    terminal: HarnessTerminal
    event_count: int = Field(ge=0)
    trace: DigestReference
    cleanup: HarnessCleanup
    candidate: DigestReference | None = None
    candidate_disposition: Literal["none", "frozen", "discarded"] = "none"
    acceptance: Literal["not_evaluated"] = "not_evaluated"
    inputs: tuple[HarnessInputEvidence, ...] = Field(default=(), max_length=2)

    @model_validator(mode="after")
    def consistent_finalization(self):
        _check_cleanup(self.attempt, self.cleanup)
        expected = "none" if self.candidate is None else "frozen" if self.terminal == "settled" else "discarded"
        if self.candidate_disposition != expected:
            raise ValueError("candidate disposition contradicts terminal or candidate identity")
        self._input_bindings()
        return self

    def _input_bindings(self):
        prior = None
        for entry in self.inputs:
            actual = (entry.attempt_id, entry.session_id, entry.contract_digest, entry.context_manifest, entry.task_digest)
            expected = (self.attempt.attempt_id, self.attempt.session_id, self.attempt.contract_digest,
                        self.attempt.context_manifest, self.attempt.digest)
            if actual != expected:
                raise ValueError("input evidence belongs to another attempt or task view")
            if entry.status == "requested":
                if prior is not None or entry.request is not None or entry.prompt_fingerprint is None:
                    raise ValueError("input request is duplicated or lacks exact content identity")
            elif (entry.status == "acknowledged" or entry.request is not None) and not entry.follows_request(prior):
                raise ValueError("input acknowledgement lacks its exact requested identity")
            prior = entry


def _action_record(event: SemanticActionEventV1) -> bytes:
    return event.model_dump_json().encode() + b"\n"


def semantic_trace_bytes(events: tuple[SemanticActionEventV1, ...]) -> bytes:
    """Serialize the exact JSONL byte stream committed by HarnessResult."""
    return b"".join(_action_record(event) for event in events)


def _validate_trace(attempt: HarnessAttempt, events: tuple[SemanticActionEventV1, ...]) -> None:
    provenance = attempt.attempt_id, attempt.session_id, attempt.contract_id, attempt.harness.fingerprint
    for sequence, event in enumerate(events, 1):
        actual = event.attempt_id, event.session_id, event.contract_id, event.harness_digest
        if event.sequence != sequence or actual != provenance:
            raise ValueError("semantic trace contains mismatched attempt provenance or ordering")


def _verified_result(result_bytes: bytes, expected_fingerprint: str) -> HarnessResult:
    try:
        result = HarnessResult.model_validate_json(result_bytes)
    except ValidationError:
        raise ValueError("invalid semantic trace result record") from None
    if result.model_dump_json().encode() != result_bytes:
        raise ValueError("semantic trace result record is not canonical")
    if result.fingerprint != expected_fingerprint:
        raise ValueError("semantic trace result does not match the expected fingerprint")
    return result


def _read_action(record: bytes) -> SemanticActionEventV1:
    try:
        event = SemanticActionEventV1.model_validate_json(record)
    except ValidationError:
        raise ValueError("invalid semantic action record") from None
    if _action_record(event) != record:
        raise ValueError("semantic action record is not canonical")
    return event


def verify_semantic_trace(
    result_bytes: bytes, trace_bytes: bytes, *, expected_result_fingerprint: str, max_bytes: int,
) -> tuple[HarnessResult, tuple[SemanticActionEventV1, ...]]:
    """Check canonical result and JSONL against an independently retained result fingerprint.

    The caller supplies a byte budget and controls access to the retained evidence.
    Verification preserves the result's candidate disposition and acceptance state.
    """
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("semantic trace read budget must be a positive byte count")
    if len(result_bytes) + len(trace_bytes) > max_bytes:
        raise ValueError("semantic trace exceeds the caller byte budget")
    result = _verified_result(result_bytes, expected_result_fingerprint)
    if result.trace.reference != SEMANTIC_TRACE_REFERENCE or result.trace.digest != content_digest(trace_bytes):
        raise ValueError("semantic trace does not match the result's artifact identity")
    events = tuple(_read_action(record) for record in BytesIO(trace_bytes))
    if result.event_count != len(events):
        raise ValueError("semantic trace event count differs from the result")
    _validate_trace(result.attempt, events)
    return result, events


class HarnessState:
    """Trusted executor supplies cleanup evidence; harness prose supplies neither."""

    def __init__(self, attempt: HarnessAttempt):
        self.attempt = attempt
        self.terminal: HarnessTerminal | None = None
        self.cancel_requested = False
        self.finished = False

    def cancel(self) -> None:
        if self.finished:
            raise ValueError("finished attempts cannot be cancelled")
        self.cancel_requested = True

    def settle(self, status: HarnessTerminal) -> None:
        if self.terminal is not None:
            raise ValueError("attempt terminal state is already recorded")
        self.terminal = "cancelled" if self.cancel_requested and status == "settled" else status

    def finish(self, events: tuple[SemanticActionEventV1, ...], cleanup: HarnessCleanup,
               candidate: DigestReference | None = None, *, inputs=()) -> HarnessResult:
        if self.finished or self.terminal is None:
            raise ValueError("attempt is unfinished or already finalized")
        self._validate_cleanup(cleanup)
        _validate_trace(self.attempt, events)
        trace_bytes = semantic_trace_bytes(events)
        disposition = "none" if candidate is None else "frozen" if self.terminal == "settled" else "discarded"
        result = HarnessResult(
            attempt=self.attempt, terminal=self.terminal, event_count=len(events), cleanup=cleanup,
            trace=DigestReference(reference=SEMANTIC_TRACE_REFERENCE, digest=content_digest(trace_bytes)),
            candidate=candidate, candidate_disposition=disposition, inputs=inputs,
        )
        self.finished = True
        return result

    def _validate_cleanup(self, cleanup):
        _check_cleanup(self.attempt, cleanup)


def resume_harness_attempt(previous: HarnessResult, previous_session: WorkSession,
                          checkpoint: WorkCheckpoint, digest: TaskDigest, contract: WorkContract,
                          identity: HarnessIdentity, attempt_id: str) -> tuple[HarnessAttempt, WorkSession]:
    # Reconstruct nested records: model_copy/model_construct must not bypass imported-result invariants.
    previous = HarnessResult.model_validate_json(previous.model_dump_json())
    if (previous_session.session_id != previous.attempt.session_id
            or previous_session.crew_id != previous.attempt.crew_id
            or previous_session.contract_id != contract.contract_id or attempt_id == previous.attempt.attempt_id
            or previous.attempt.contract_digest != content_digest(contract.model_dump_json().encode())):
        raise ValueError("resume requires exact previous identity and a fresh attempt")
    if not previous.cleanup.process_tree_stopped:
        raise ValueError("previous cleanup must complete before workspace reuse")
    session = resume_session(previous_session, checkpoint)
    attempt = harness_attempt(contract, session, identity, attempt_id, digest,
                              attempt_number=previous.attempt.attempt_number + 1)
    return attempt.model_copy(update={
        "previous_attempt": DigestReference(reference="artifact:harness-attempt", digest=previous.fingerprint),
        "checkpoint": DigestReference(reference="artifact:work-checkpoint", digest=content_digest(checkpoint.model_dump_json().encode())),
    }), session


@runtime_checkable
class CodingHarnessAdapter(Protocol):
    identity: HarnessIdentity

    def start(self, attempt: HarnessAttempt) -> None: ...
    def poll(self, now: datetime, monotonic_ns: int) -> tuple[SemanticActionEventV1, ...]: ...
    def cancel(self) -> None: ...
    def finish(self, cleanup: HarnessCleanup, candidate: DigestReference | None = None) -> HarnessResult: ...
