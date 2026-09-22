"""Pi coding-harness adapter boundary. Pi-specific protocol stays at this edge."""
from __future__ import annotations

from enum import Enum
from typing import Protocol

from pydantic import BaseModel, Field

from ..work import (
    AttemptStep,
    ProgressKind,
    StepOutcome,
    TaskDigest,
    WorkContract,
)


class PiEventKind(str, Enum):
    TOOL = "tool"
    MESSAGE = "message"
    RESULT = "result"
    INTERRUPTION = "interruption"


class PiEvent(BaseModel):
    kind: PiEventKind
    action: str
    arguments: dict[str, object] = Field(default_factory=dict)
    success: bool | None = None
    result_fingerprint: str | None = None
    artifact_fingerprint: str | None = None
    detail: str = ""


class PiRequest(BaseModel):
    contract_id: str
    session_id: str
    objective: str
    context: str
    allowed_paths: list[str]
    capabilities: list[str]
    timeout_seconds: float = Field(gt=0)


class PiRunResult(BaseModel):
    events: list[PiEvent] = Field(default_factory=list)
    interrupted: bool = False
    error: str | None = None


class PiBackend(Protocol):
    def run(self, request: PiRequest) -> PiRunResult: ...


class PiAttemptResult(BaseModel):
    request: PiRequest
    steps: list[AttemptStep]
    artifact_fingerprint: str | None = None
    interrupted: bool = False
    resumable: bool = False
    error: str | None = None


class PiAdapter:
    """Translate Long Haul contracts to/from a Pi backend without changing task semantics."""

    def __init__(self, backend: PiBackend):
        self.backend = backend

    def build_request(self, contract: WorkContract, session_id: str, digest: TaskDigest | None = None) -> PiRequest:
        context_parts = [contract.context.strip()]
        if digest is not None:
            if digest.contract_id != contract.contract_id:
                raise ValueError("digest belongs to a different work contract")
            context_parts.append(digest.text)
        context = "\n\n".join(part for part in context_parts if part)
        return PiRequest(
            contract_id=contract.contract_id,
            session_id=session_id,
            objective=contract.objective,
            context=context,
            allowed_paths=contract.allowed_paths,
            capabilities=contract.capabilities,
            timeout_seconds=contract.budget.wall_seconds,
        )

    def execute(self, contract: WorkContract, session_id: str, digest: TaskDigest | None = None) -> PiAttemptResult:
        request = self.build_request(contract, session_id, digest)
        result = self.backend.run(request)
        steps: list[AttemptStep] = []
        artifact: str | None = None
        for index, event in enumerate(result.events):
            if event.artifact_fingerprint:
                artifact = event.artifact_fingerprint
            if event.kind is PiEventKind.MESSAGE:
                continue
            if event.kind is PiEventKind.INTERRUPTION:
                progress = ProgressKind.NO_PROGRESS
                outcome = StepOutcome.FAILURE
            elif event.kind is PiEventKind.RESULT:
                progress = ProgressKind.VERIFICATION
                outcome = StepOutcome.SUCCESS if event.success else StepOutcome.FAILURE
            else:
                progress = self._classify_tool(event.action)
                outcome = StepOutcome.SUCCESS if event.success else StepOutcome.FAILURE if event.success is False else StepOutcome.UNKNOWN
            steps.append(
                AttemptStep(
                    step_id=f"{session_id}:{index}",
                    action=event.action,
                    arguments=event.arguments,
                    outcome=outcome,
                    progress=progress,
                    result_fingerprint=event.result_fingerprint,
                )
            )
        return PiAttemptResult(
            request=request,
            steps=steps,
            artifact_fingerprint=artifact,
            interrupted=result.interrupted,
            resumable=result.interrupted,
            error=result.error,
        )

    @staticmethod
    def _classify_tool(action: str) -> ProgressKind:
        name = action.lower()
        if any(token in name for token in ("write", "edit", "patch", "create", "delete")):
            return ProgressKind.STATE_CHANGE
        if any(token in name for token in ("test", "lint", "build", "check", "verify")):
            return ProgressKind.VERIFICATION
        return ProgressKind.INFORMATION


class FakePiBackend:
    """Hermetic scripted backend for CI and reference-vessel qualification."""

    def __init__(self, events: list[PiEvent], interrupted: bool = False, error: str | None = None):
        self.events = events
        self.interrupted = interrupted
        self.error = error
        self.requests: list[PiRequest] = []

    def run(self, request: PiRequest) -> PiRunResult:
        self.requests.append(request)
        return PiRunResult(events=self.events, interrupted=self.interrupted, error=self.error)
