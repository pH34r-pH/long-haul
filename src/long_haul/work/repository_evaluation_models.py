"""Frozen repository evaluator profiles and evidence, composing v1 work evidence."""
from __future__ import annotations

import platform
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import EvidenceState, WorkContract, WorkEvaluation
from .evaluator import EvidencePacket, evaluate_packet
from .repository_models import (
    DigestReference,
    FrozenProjection,
    RepositoryIdentity,
    repository_path,
)


def work_contract_fingerprint(contract: WorkContract) -> str:
    from ..resource_policy_models import canonical_json
    from .repository_models import content_digest
    return content_digest(canonical_json(contract.model_dump(mode="json")).encode())


def evaluator_check_fingerprint(profile, check):
    """Use keyed identities for private profiles; preserve historical v1 hashes."""
    from .private_evaluator import PrivateEvaluatorProfile
    return profile.check_fingerprint(check) if isinstance(profile, PrivateEvaluatorProfile) else check.fingerprint


def evaluator_implementation_reference() -> DigestReference:
    import pydantic

    from ..resource_policy_models import canonical_json
    from .repository_models import content_digest
    root = Path(__file__).parent
    files = (
        "repository_evaluation_models.py", "repository_evaluator.py", "repository_candidate.py", "repository_sandbox.py",
        "contracts.py", "evaluator.py", "repository_models.py", "repository_git.py", "private_evaluator.py",
        "repository_evidence_admission.py",
    )
    manifest = {name: content_digest((root / name).read_bytes()) for name in files}
    # Public and private evidence admission uses the shared policy implementation.
    for name in ("resource_policy.py", "resource_policy_models.py", "resource_policy_evaluator.py"):
        manifest["../" + name] = content_digest((root.parent / name).read_bytes())
    data = canonical_json({"files": manifest, "python": platform.python_version(), "pydantic": pydantic.__version__}).encode()
    return DigestReference(reference="urn:long-haul:repository-evaluator-v1", digest=content_digest(data))


class SandboxLimits(FrozenProjection):
    wall_seconds: float = Field(default=10, gt=0, le=300)
    memory_mib: int = Field(default=256, ge=64, le=4096)
    cpus: float = Field(default=1, gt=0, le=4)
    pids: int = Field(default=32, ge=8, le=128)
    scratch_mib: int = Field(default=16, ge=1, le=256)
    max_output_bytes: int = Field(default=16384, ge=1, le=1048576)


class EvaluatorEnvironment(FrozenProjection):
    image: str = Field(pattern=r"^[a-z0-9./:_-]+@sha256:[0-9a-f]{64}$")
    runtime: str = Field(default="long-haul-gvisor", pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    runtime_version: str = Field(min_length=1, max_length=128)
    runtime_entrypoint: str = "runsc"
    runtime_archive_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_binary_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_files: tuple[tuple[str, str], ...] = Field(min_length=1, max_length=32)
    network: Literal["none"] = "none"
    candidate_access: Literal["read-only"] = "read-only"
    toolchain: str = Field(default="isolated-python3", min_length=1, max_length=128)
    limits: SandboxLimits = Field(default_factory=SandboxLimits)

    @model_validator(mode="after")
    def pinned_runtime_files(self):
        import re
        if len(dict(self.runtime_files)) != len(self.runtime_files):
            raise ValueError("runtime file paths must be unique")
        for path, digest in self.runtime_files:
            repository_path(path)
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("runtime file requires SHA-256 fingerprint")
        repository_path(self.runtime_entrypoint)
        if dict(self.runtime_files).get(self.runtime_entrypoint) != self.runtime_binary_digest:
            raise ValueError("runtime binary must be included in the pinned tool bundle")
        return self


class AcceptanceRequirement(FrozenProjection):
    predicate_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=512)
    classification: Literal["PUBLIC", "SEALED", "POLICY", "INSPECTION"]


class CandidateCommand(FrozenProjection):
    program: str
    arguments: tuple[str, ...] = Field(default=(), max_length=32)
    stdin: str = Field(default="", max_length=16384)
    expected_stdout_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    expected_exit_code: int = Field(default=0, ge=0, le=255)
    result_schema: Literal["bounded-bytes-v1"] = "bounded-bytes-v1"
    _path = field_validator("program")(repository_path)

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, arguments):
        if any(len(value) > 512 or "\x00" in value for value in arguments):
            raise ValueError("candidate argument is invalid or too large")
        return arguments


class RepositoryCheck(FrozenProjection):
    requirement: AcceptanceRequirement
    kind: Literal["command", "scope", "artifact"]
    command: CandidateCommand | None = None
    artifact_path: str | None = None
    artifact_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def check_shape(self):
        shape = (self.kind, self.requirement.classification, self.command is None,
                 self.artifact_path is None, self.artifact_digest is None)
        allowed = {("command", "PUBLIC", False, True, True), ("command", "SEALED", False, True, True),
                   ("scope", "POLICY", True, True, True), ("artifact", "INSPECTION", True, False, False)}
        if shape not in allowed:
            raise ValueError("check class and implementation shape disagree")
        if self.artifact_path is not None:
            repository_path(self.artifact_path)
        return self


class RepositoryEvaluationProfileV1(FrozenProjection):
    schema_version: Literal[1] = 1
    profile: Literal["repository-evaluation-v1"] = "repository-evaluation-v1"
    evaluator_id: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(min_length=1, max_length=64)
    task: DigestReference
    environment: EvaluatorEnvironment
    implementation: DigestReference
    sandbox: DigestReference
    checks: tuple[RepositoryCheck, ...] = Field(min_length=1, max_length=128)
    deterministic: Literal[True] = True
    seed: Literal[0] = 0
    publication_policy: Literal["requirements-and-hashed-outcomes"] = "requirements-and-hashed-outcomes"

    @field_validator("checks")
    @classmethod
    def unique_checks(cls, checks):
        if len({check.requirement.predicate_id for check in checks}) != len(checks):
            raise ValueError("evaluator predicate ids must be unique")
        return checks

    def public_projection(self) -> dict:
        return {"profile": self.profile, "evaluator_id": self.evaluator_id,
                "evaluator_version": self.evaluator_version, "task": self.task.model_dump(mode="json"),
                "requirements": [check.requirement.model_dump(mode="json") for check in self.checks],
                "public_checks": [check.model_dump(mode="json") for check in self.checks
                                  if check.requirement.classification == "PUBLIC"]}


def _candidate_file_path(value):
    value = repository_path(value)
    if value == ".":
        raise ValueError("candidate path must identify a repository file")
    return value


class CandidateFile(FrozenProjection):
    path: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, le=1048576)
    executable: bool = False
    _path = field_validator("path")(_candidate_file_path)


class FrozenRepositoryCandidate(FrozenProjection):
    schema_version: Literal[1] = 1
    profile: Literal["frozen-repository-candidate-v1"] = "frozen-repository-candidate-v1"
    base: RepositoryIdentity
    source: RepositoryIdentity
    files: tuple[CandidateFile, ...] = Field(max_length=256)
    changed_paths: tuple[str, ...] = Field(max_length=512)
    contract_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @field_validator("changed_paths")
    @classmethod
    def safe_changed_paths(cls, paths):
        normalized = tuple(_candidate_file_path(path) for path in paths)
        if len(set(normalized)) != len(normalized):
            raise ValueError("changed paths must be unique")
        return normalized

    @model_validator(mode="after")
    def exact_files(self):
        if len({item.path for item in self.files}) != len(self.files):
            raise ValueError("candidate file paths must be unique")
        if sum(item.size_bytes for item in self.files) > 4194304:
            raise ValueError("candidate exceeds total byte limit")
        if ((self.base.repository_id, self.base.visibility, self.base.data_class)
                != (self.source.repository_id, self.source.visibility, self.source.data_class)):
            raise ValueError("candidate and base must identify the same repository and classification")
        return self


class SandboxExecutionReceipt(FrozenProjection):
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    resolved_image_id: str | None = None
    outcome: Literal["completed", "invalid", "timeout", "output_limit", "resource_exhausted"]
    cleanup: Literal["removed", "failed"]
    process_stopped: bool | None = None
    startup_verified: bool = False
    stdout_digest: str | None = None
    stderr_digest: str | None = None
    exit_code: int | None = None
    provenance: Literal["runtime-observation", "synthetic-fixture"] = "runtime-observation"


class RepositoryCheckEvidence(FrozenProjection):
    predicate_id: str
    classification: Literal["PUBLIC", "SEALED", "POLICY", "INSPECTION"]
    check_fingerprint: str
    state: EvidenceState
    outcome: Literal["pass", "fail", "invalid", "timeout", "output_limit", "resource_exhausted"]
    stdout_digest: str | None = None
    stderr_digest: str | None = None
    exit_code: int | None = None
    started_at: datetime
    ended_at: datetime
    duration_seconds: float = Field(ge=0)
    sandbox_receipt: DigestReference | None = None
    sandbox_execution: SandboxExecutionReceipt | None = None
    diagnostic: Literal["sandbox_invalid", "cleanup_failed"] | None = None

    @model_validator(mode="after")
    def consistent_outcome(self):
        expected = {"pass": EvidenceState.PASS, "fail": EvidenceState.FAIL}.get(self.outcome, EvidenceState.UNKNOWN)
        if self.state is not expected:
            raise ValueError("check state contradicts execution outcome")
        if (self.started_at.tzinfo is None or self.ended_at.tzinfo is None
                or self.ended_at < self.started_at):
            raise ValueError("check timestamps must be ordered and timezone-aware")
        if (self.classification in {"POLICY", "INSPECTION"}
                and any(value is not None for value in (self.sandbox_receipt, self.sandbox_execution,
                                                        self.stdout_digest, self.stderr_digest, self.exit_code))):
            raise ValueError("non-command predicates cannot carry command output/receipts")
        if self.sandbox_execution is not None:
            expected = DigestReference(reference="urn:long-haul:sandbox:" + self.sandbox_execution.container_id,
                                       digest=self.sandbox_execution.fingerprint)
            if self.sandbox_receipt != expected:
                raise ValueError("sandbox receipt does not match retained execution facts")
            actual = (self.stdout_digest, self.stderr_digest, self.exit_code)
            observed = (self.sandbox_execution.stdout_digest, self.sandbox_execution.stderr_digest,
                        self.sandbox_execution.exit_code)
            if actual != observed:
                raise ValueError("check output contradicts retained sandbox facts")
            if self.state is EvidenceState.PASS and self.sandbox_execution.cleanup != "removed":
                raise ValueError("unresolved sandbox cleanup cannot pass")
            _consistent_execution_outcome(self)
        elif self.classification in {"PUBLIC", "SEALED"} and self.outcome in {"pass", "fail"}:
            raise ValueError("completed command checks require retained sandbox execution facts")
        return self


def _consistent_execution_outcome(check):
    execution = check.sandbox_execution
    expected = "completed" if check.outcome in {"pass", "fail"} else check.outcome
    if execution.outcome != expected:
        raise ValueError("check outcome contradicts sandbox execution outcome")
    if check.outcome in {"pass", "fail"}:
        required = (execution.cleanup == "removed", execution.process_stopped is True,
                    execution.stdout_digest is not None, execution.stderr_digest is not None,
                    execution.exit_code is not None, execution.startup_verified)
        if not all(required):
            raise ValueError("completed command execution facts are incomplete")
        if execution.provenance == "runtime-observation" and execution.resolved_image_id is None:
            raise ValueError("runtime outcome lacks exact resolved image identity")
    if check.classification in {"POLICY", "INSPECTION"}:
        raise ValueError("non-command predicates cannot carry command execution facts")


class RepositoryEvidencePacketV1(FrozenProjection):
    schema_version: Literal[1] = 1
    profile: Literal["repository-evidence-v1"] = "repository-evidence-v1"
    evidence: EvidencePacket
    candidate: FrozenRepositoryCandidate
    task: DigestReference
    context_manifest: DigestReference
    evaluator_profile: DigestReference
    evaluator_environment: DigestReference
    sandbox_implementation: DigestReference
    contract_fingerprint: str
    checks: tuple[RepositoryCheckEvidence, ...]
    validity: Literal["valid", "invalid"]
    invalid_reasons: tuple[str, ...] = ()
    producing_attempt_references: tuple[DigestReference, ...] = ()

    @model_validator(mode="after")
    def unique_observations(self):
        self.evidence.unique_checks()
        ids = [check.predicate_id for check in self.checks]
        if len(ids) != len(set(ids)) or set(ids) != {c.predicate_id for c in self.evidence.checks}:
            raise ValueError("repository evidence must have one exact observation per predicate")
        _consistent_legacy_evidence(self)
        invalid_outcomes = any(c.outcome not in {"pass", "fail"} for c in self.checks)
        if self.validity == "valid" and (invalid_outcomes or self.invalid_reasons):
            raise ValueError("invalid execution cannot be declared valid")
        return self


def _consistent_legacy_evidence(packet):
    actual = ({c.predicate_id: c.state for c in packet.evidence.checks}, packet.evidence.candidate.fingerprint,
              packet.evidence.candidate.reference, packet.candidate.contract_fingerprint,
              set(packet.evidence.verified), set(packet.evidence.unresolved))
    expected = ({c.predicate_id: c.state for c in packet.checks}, packet.candidate.fingerprint,
                "git:" + packet.candidate.source.commit, packet.contract_fingerprint,
                {c.predicate_id for c in packet.checks if c.state is EvidenceState.PASS},
                {c.predicate_id for c in packet.checks if c.state is EvidenceState.UNKNOWN})
    if actual != expected:
        raise ValueError("composed v1 evidence contradicts repository evidence identity/state")
    for check in packet.checks:
        if (check.sandbox_execution is not None
                and check.sandbox_execution.environment_fingerprint != packet.evaluator_environment.digest):
            raise ValueError("sandbox facts refer to another environment")
        if (packet.sandbox_implementation.reference == "urn:long-haul:docker-gvisor-v1"
                and check.sandbox_execution is not None
                and check.sandbox_execution.provenance != "runtime-observation"):
            raise ValueError("fixture provenance cannot assert real sandbox qualification")


def _packet_binding_matches(contract, packet, candidate, profile):
    from .repository_evaluator import public_evaluator_reference
    extension = contract.repository_work
    if extension is None or extension.sealed_evaluator is None:
        return False
    actual = (packet.contract_fingerprint, packet.candidate, packet.task, packet.evaluator_profile.digest,
              packet.evaluator_environment.digest, packet.sandbox_implementation, packet.context_manifest,
              packet.candidate.base, extension.public_evaluation, extension.sealed_evaluator.digest,
              packet.evidence.evaluator.evaluator_id, packet.evidence.evaluator.version, packet.evidence.evaluator.runtime,
              profile.implementation)
    expected = (work_contract_fingerprint(contract), candidate, profile.task, profile.fingerprint,
                profile.environment.fingerprint, profile.sandbox, extension.context_manifest, extension.source,
                public_evaluator_reference(profile), profile.fingerprint, profile.evaluator_id,
                profile.evaluator_version, profile.environment.fingerprint, evaluator_implementation_reference())
    return actual == expected


def evaluate_repository_packet(contract, packet, candidate, evaluator_profile) -> WorkEvaluation:
    packet = RepositoryEvidencePacketV1.model_validate(packet.model_dump())
    mismatch = not _packet_binding_matches(contract, packet, candidate, evaluator_profile)
    if mismatch or packet.validity == "invalid":
        return WorkEvaluation(contract_id=contract.contract_id, disposition="INCOMPLETE",
                              evidence=packet.evidence.predicate_evidence(),
                              reasons=["invalid repository evaluation" if packet.validity == "invalid"
                                       else "stale task, contract, candidate, evaluator or environment identity"])
    expected = {c.requirement.predicate_id: (evaluator_check_fingerprint(evaluator_profile, c), c.requirement.classification)
                for c in evaluator_profile.checks}
    actual = {c.predicate_id: (c.check_fingerprint, c.classification) for c in packet.checks}
    if actual != expected:
        raise ValueError("observation does not match the frozen evaluator check")
    _validate_check_results(contract, packet, evaluator_profile)
    return evaluate_packet(contract, packet.evidence, candidate.fingerprint)


def _validate_check_results(contract, packet, profile):
    by_id = {c.predicate_id: c for c in packet.checks}
    for check in profile.checks:
        observed = by_id[check.requirement.predicate_id]
        if check.kind == "command":
            execution = observed.sandbox_execution
            correct = (execution.stdout_digest == check.command.expected_stdout_digest
                       and execution.exit_code == check.command.expected_exit_code)
            expected = EvidenceState.PASS if correct else EvidenceState.FAIL
        elif check.kind == "scope":
            correct = all(contract.permits_path(path) for path in packet.candidate.changed_paths)
            expected = EvidenceState.PASS if correct else EvidenceState.FAIL
        else:
            correct = {item.path: item.digest for item in packet.candidate.files}.get(check.artifact_path) == check.artifact_digest
            expected = EvidenceState.PASS if correct else EvidenceState.FAIL
        if observed.state is not expected:
            raise ValueError("predicate state contradicts the frozen check's expected behavior")
