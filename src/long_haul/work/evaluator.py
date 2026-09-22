"""Independent read-only acceptance evaluation bound to exact candidate artifacts."""
from __future__ import annotations

from enum import Enum
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .contracts import (
    EvidenceState,
    PredicateEvidence,
    WorkContract,
    WorkEvaluation,
    evaluate_work,
)

EVIDENCE_PACKET_SCHEMA_VERSION = 1


class EvaluationAuthority(str, Enum):
    READ_ONLY = "read_only"


class CandidateArtifact(BaseModel):
    fingerprint: str = Field(min_length=1)
    reference: str = Field(min_length=1)


class EvaluatorIdentity(BaseModel):
    evaluator_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    runtime: str = Field(min_length=1)


class CheckRecord(BaseModel):
    predicate_id: str = Field(min_length=1)
    command: list[str] = Field(default_factory=list)
    state: EvidenceState
    detail: str = ""


class EvidencePacket(BaseModel):
    packet_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    candidate: CandidateArtifact
    evaluator: EvaluatorIdentity
    authority: Literal[EvaluationAuthority.READ_ONLY] = EvaluationAuthority.READ_ONLY
    checks: list[CheckRecord] = Field(default_factory=list)
    verified: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    regressions: list[str] = Field(default_factory=list)
    schema_version: Literal[1] = EVIDENCE_PACKET_SCHEMA_VERSION

    def predicate_evidence(self) -> list[PredicateEvidence]:
        return [
            PredicateEvidence(
                predicate_id=check.predicate_id,
                state=check.state,
                source=f"evidence:{self.packet_id}",
                detail=check.detail,
                artifact_fingerprint=self.candidate.fingerprint,
            )
            for check in self.checks
        ]


class ReadOnlyEvaluationTarget(Protocol):
    @property
    def fingerprint(self) -> str: ...

    def inspect(self, path: str) -> str: ...

    def run_check(self, command: list[str]) -> tuple[EvidenceState, str]: ...


class EvaluationRunner:
    """Runs contract predicates against an interface with no mutation method."""

    def __init__(self, target: ReadOnlyEvaluationTarget, evaluator: EvaluatorIdentity):
        self.target = target
        self.evaluator = evaluator

    def evaluate(
        self,
        contract: WorkContract,
        attempt_id: str,
        packet_id: str,
        candidate_reference: str,
    ) -> tuple[EvidencePacket, WorkEvaluation]:
        fingerprint = self.target.fingerprint
        checks: list[CheckRecord] = []
        for predicate in contract.success_predicates:
            if predicate.command:
                state, detail = self.target.run_check(predicate.command)
            elif predicate.artifact_path:
                try:
                    self.target.inspect(predicate.artifact_path)
                    state, detail = EvidenceState.PASS, "artifact present and inspectable"
                except (FileNotFoundError, PermissionError) as exc:
                    state, detail = EvidenceState.FAIL, str(exc)
            else:
                state, detail = EvidenceState.UNKNOWN, "predicate has no evaluator action"
            checks.append(CheckRecord(predicate_id=predicate.predicate_id, command=predicate.command, state=state, detail=detail))
        packet = EvidencePacket(
            packet_id=packet_id,
            contract_id=contract.contract_id,
            attempt_id=attempt_id,
            candidate=CandidateArtifact(fingerprint=fingerprint, reference=candidate_reference),
            evaluator=self.evaluator,
            checks=checks,
            verified=[item.predicate_id for item in checks if item.state is EvidenceState.PASS],
            unresolved=[item.predicate_id for item in checks if item.state is EvidenceState.UNKNOWN],
        )
        return packet, evaluate_packet(contract, packet, fingerprint)


def evaluate_packet(contract: WorkContract, packet: EvidencePacket, candidate_fingerprint: str) -> WorkEvaluation:
    if packet.contract_id != contract.contract_id:
        raise ValueError("evidence packet belongs to a different work contract")
    if packet.candidate.fingerprint != candidate_fingerprint:
        evidence = packet.predicate_evidence()
        return WorkEvaluation(
            contract_id=contract.contract_id,
            disposition="INCOMPLETE",
            evidence=evidence,
            reasons=["stale evidence: candidate fingerprint changed"],
        )
    return evaluate_work(contract, packet.predicate_evidence())


def reopen_regressions(previous: EvidencePacket, current: EvidencePacket) -> EvidencePacket:
    """Mark predicates that were verified previously but fail on the new candidate."""
    previous_pass = {item.predicate_id for item in previous.checks if item.state is EvidenceState.PASS}
    current_fail = {item.predicate_id for item in current.checks if item.state is EvidenceState.FAIL}
    current.regressions = sorted(previous_pass & current_fail)
    return current
