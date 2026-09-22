"""Externally verifiable, model-neutral engineering work contracts."""
from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, model_validator

WORK_CONTRACT_SCHEMA_VERSION = 1


def _validate_scope(path: str) -> str:
    value = path.strip().replace("\\", "/")
    if not value or value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise ValueError("work scope paths must be non-empty repository-relative paths without '..'")
    return value.rstrip("/")


class PredicateKind(str, Enum):
    TEST = "test"
    BUILD = "build"
    LINT = "lint"
    STATIC_CHECK = "static_check"
    ARTIFACT = "artifact"


class FailureKind(str, Enum):
    SCOPE_VIOLATION = "scope_violation"
    TIMEOUT = "timeout"
    MALFORMED_ACTION = "malformed_action"
    NO_PROGRESS = "no_progress"
    RESOURCE_EXHAUSTION = "resource_exhaustion"


class EvidenceState(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class WorkDisposition(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    INCOMPLETE = "INCOMPLETE"


class WorkBudget(BaseModel):
    wall_seconds: float = Field(gt=0)
    max_attempts: int = Field(default=1, ge=1)
    max_tokens_per_attempt: int | None = Field(default=None, ge=1)
    max_no_progress_steps: int | None = Field(default=None, ge=1)


class AcceptancePredicate(BaseModel):
    predicate_id: str = Field(min_length=1)
    kind: PredicateKind
    description: str = Field(min_length=1)
    command: list[str] = Field(default_factory=list)
    artifact_path: str | None = None

    @model_validator(mode="after")
    def executable_or_artifact(self) -> "AcceptancePredicate":
        if self.kind is PredicateKind.ARTIFACT:
            if not self.artifact_path:
                raise ValueError("artifact predicates require artifact_path")
            self.artifact_path = _validate_scope(self.artifact_path)
        elif not self.command:
            raise ValueError(f"{self.kind.value} predicates require a command")
        return self


class FailurePredicate(BaseModel):
    kind: FailureKind
    limit: int | float | None = None


class WorkContract(BaseModel):
    contract_id: str = Field(min_length=1)
    mission_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    context: str = ""
    execution_plan_id: str | None = None
    allowed_paths: list[str] = Field(min_length=1)
    forbidden_paths: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    authority_scopes: list[str] = Field(default_factory=list)
    budget: WorkBudget
    success_predicates: list[AcceptancePredicate] = Field(min_length=1)
    failure_predicates: list[FailurePredicate] = Field(default_factory=list)
    schema_version: Literal[1] = WORK_CONTRACT_SCHEMA_VERSION

    @model_validator(mode="after")
    def validate_contract(self) -> "WorkContract":
        self.allowed_paths = [_validate_scope(path) for path in self.allowed_paths]
        self.forbidden_paths = [_validate_scope(path) for path in self.forbidden_paths]
        if len(set(self.allowed_paths)) != len(self.allowed_paths):
            raise ValueError("allowed_paths must be unique")
        if len(set(self.forbidden_paths)) != len(self.forbidden_paths):
            raise ValueError("forbidden_paths must be unique")
        if len({p.predicate_id for p in self.success_predicates}) != len(self.success_predicates):
            raise ValueError("success predicate ids must be unique")
        return self

    def permits_path(self, path: str) -> bool:
        candidate = _validate_scope(path)
        def within(root: str) -> bool:
            return candidate == root or candidate.startswith(root + "/")
        return any(within(root) for root in self.allowed_paths) and not any(within(root) for root in self.forbidden_paths)


class PredicateEvidence(BaseModel):
    predicate_id: str
    state: EvidenceState
    source: str = Field(min_length=1)
    detail: str = ""
    artifact_fingerprint: str | None = None


class WorkEvaluation(BaseModel):
    contract_id: str
    disposition: WorkDisposition
    evidence: list[PredicateEvidence]
    reasons: list[str] = Field(default_factory=list)


def evaluate_work(contract: WorkContract, evidence: list[PredicateEvidence], failure: FailureKind | None = None) -> WorkEvaluation:
    """Evaluate only external evidence. Worker/model prose is never an acceptance input."""
    by_id = {item.predicate_id: item for item in evidence}
    expected = {item.predicate_id for item in contract.success_predicates}
    reasons: list[str] = []
    if failure is not None:
        reasons.append(f"failure predicate triggered: {failure.value}")
        return WorkEvaluation(contract_id=contract.contract_id, disposition=WorkDisposition.REJECTED, evidence=evidence, reasons=reasons)
    failed = [pid for pid in expected if pid in by_id and by_id[pid].state is EvidenceState.FAIL]
    missing = [pid for pid in expected if pid not in by_id or by_id[pid].state is EvidenceState.UNKNOWN]
    if failed:
        reasons.append("failed predicates: " + ", ".join(sorted(failed)))
        disposition = WorkDisposition.REJECTED
    elif missing:
        reasons.append("unverified predicates: " + ", ".join(sorted(missing)))
        disposition = WorkDisposition.INCOMPLETE
    else:
        disposition = WorkDisposition.ACCEPTED
    return WorkEvaluation(contract_id=contract.contract_id, disposition=disposition, evidence=evidence, reasons=reasons)
