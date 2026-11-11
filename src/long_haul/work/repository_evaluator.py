"""Independent public/sealed checks against immutable, read-only candidates."""
from __future__ import annotations

import time
from datetime import UTC, datetime

from ..events.store import Event
from ..resource_policy_models import canonical_json
from .contracts import EvidenceState, WorkContract
from .evaluator import CandidateArtifact, CheckRecord, EvaluatorIdentity, EvidencePacket
from .repository_candidate import CandidateSnapshot
from .repository_evaluation_models import (
    RepositoryCheckEvidence,
    RepositoryEvaluationProfileV1,
    RepositoryEvidencePacketV1,
    evaluate_repository_packet,
    evaluator_implementation_reference,
    work_contract_fingerprint,
)
from .repository_models import DigestReference, content_digest
from .repository_sandbox import SandboxAdapter, SandboxResult


def public_evaluator_reference(profile: RepositoryEvaluationProfileV1) -> DigestReference:
    return DigestReference(reference="urn:long-haul:repository-evaluator:public",
                           digest=content_digest(canonical_json(profile.public_projection()).encode()))


def _validate_binding(contract, snapshot, profile, sandbox):
    extension = contract.repository_work
    if extension is None or extension.sealed_evaluator is None:
        raise ValueError("repository evaluator requires an exact evaluator profile reference")
    actual = (extension.task, extension.public_evaluation, extension.sealed_evaluator.digest,
              snapshot.candidate.base, snapshot.candidate.contract_fingerprint,
              sandbox.environment, sandbox.identity, profile.implementation)
    expected = (profile.task, public_evaluator_reference(profile), profile.fingerprint,
                extension.source, work_contract_fingerprint(contract), profile.environment,
                profile.sandbox, evaluator_implementation_reference())
    if actual != expected:
        raise ValueError("repository evaluator task/source/contract/profile/environment binding mismatch")
    predicates = {predicate.predicate_id: predicate for predicate in contract.success_predicates}
    if set(predicates) != {check.requirement.predicate_id for check in profile.checks}:
        raise ValueError("evaluator requirements differ from work contract")
    for check in profile.checks:
        requirement = check.requirement
        reference = DigestReference(reference="urn:long-haul:evaluator-requirement:" + requirement.predicate_id,
                                    digest=check.fingerprint)
        if (predicates[requirement.predicate_id].evaluator_reference != reference
                or predicates[requirement.predicate_id].description != requirement.description):
            raise ValueError("repository predicates must use exact evaluator requirement references")
    if not any(check.kind == "scope" for check in profile.checks):
        raise ValueError("repository evaluator requires trusted scope evidence")
    commands = sum(check.kind == "command" for check in profile.checks)
    if commands * profile.environment.limits.wall_seconds > contract.budget.wall_seconds:
        raise ValueError("evaluator worst-case wall budget exceeds work contract")


def _result_state(result, command):
    if result.outcome != "completed" or result.receipt is None:
        outcome = result.outcome if result.outcome in {"timeout", "output_limit", "resource_exhausted"} else "invalid"
        return EvidenceState.UNKNOWN, outcome
    correct = (result.exit_code == command.expected_exit_code
               and content_digest(result.stdout) == command.expected_stdout_digest)
    return (EvidenceState.PASS, "pass") if correct else (EvidenceState.FAIL, "fail")


def _sandbox_observation(result):
    if result is None:
        return {}
    observed = result.receipt is not None and result.outputs_observed
    return {"stdout_digest": content_digest(result.stdout) if observed else None,
            "stderr_digest": content_digest(result.stderr) if observed else None,
            "exit_code": result.exit_code, "sandbox_receipt": result.receipt,
            "sandbox_execution": result.execution_receipt, "diagnostic": result.diagnostic}


def _observe_check(check, snapshot, target, sandbox, scope_valid):
    started = datetime.now(UTC)
    result = None
    if check.kind == "scope":
        state, outcome = (EvidenceState.PASS, "pass") if scope_valid else (EvidenceState.FAIL, "fail")
    elif not scope_valid:
        state, outcome = EvidenceState.UNKNOWN, "invalid"
    elif check.kind == "artifact":
        observed = {item.path: item.digest for item in snapshot.candidate.files}.get(check.artifact_path)
        state, outcome = ((EvidenceState.PASS, "pass") if observed == check.artifact_digest
                          else (EvidenceState.FAIL, "fail"))
    else:
        try:
            result = sandbox.execute(target, check.command)
        except (OSError, ValueError, RuntimeError):
            result = SandboxResult("invalid")
        state, outcome = _result_state(result, check.command)
    ended = datetime.now(UTC)
    return RepositoryCheckEvidence(
        predicate_id=check.requirement.predicate_id, classification=check.requirement.classification,
        check_fingerprint=check.fingerprint, state=state, outcome=outcome,
        **_sandbox_observation(result),
        started_at=started, ended_at=ended, duration_seconds=(ended - started).total_seconds(),
    )


def _legacy_evidence(contract, snapshot, profile, observations, attempt_id, packet_id):
    return EvidencePacket(
        packet_id=packet_id, contract_id=contract.contract_id, attempt_id=attempt_id,
        candidate=CandidateArtifact(fingerprint=snapshot.candidate.fingerprint,
                                    reference="git:" + snapshot.candidate.source.commit),
        evaluator=EvaluatorIdentity(evaluator_id=profile.evaluator_id, version=profile.evaluator_version,
                                    runtime=profile.environment.fingerprint),
        checks=[CheckRecord(predicate_id=item.predicate_id, command=[], state=item.state,
                            detail="repository check " + item.outcome) for item in observations],
        verified=[item.predicate_id for item in observations if item.state is EvidenceState.PASS],
        unresolved=[item.predicate_id for item in observations if item.state is EvidenceState.UNKNOWN],
    )


class RepositoryEvaluationRunner:
    """Sealed definitions remain here; untrusted code receives only current SUT inputs."""

    def __init__(self, sandbox: SandboxAdapter):
        self.sandbox = sandbox

    def evaluate(self, contract: WorkContract, snapshot: CandidateSnapshot,
                 profile: RepositoryEvaluationProfileV1, *, attempt_id: str, packet_id: str):
        _validate_binding(contract, snapshot, profile, self.sandbox)
        started = time.monotonic()
        scope_valid = all(contract.permits_path(path) for path in snapshot.candidate.changed_paths)
        with snapshot.materialize() as target:
            observations = tuple(_observe_check(check, snapshot, target, self.sandbox, scope_valid)
                                 for check in profile.checks)
        invalid = tuple(check.predicate_id + ":" + check.outcome + (":" + check.diagnostic if check.diagnostic else "") for check in observations
                        if check.outcome not in {"pass", "fail"})
        if not scope_valid:
            invalid += ("candidate scope violation",)
        if time.monotonic() - started > contract.budget.wall_seconds:
            invalid += ("evaluator exceeded work-contract wall budget",)
        evidence = _legacy_evidence(contract, snapshot, profile, observations, attempt_id, packet_id)
        packet = RepositoryEvidencePacketV1(
            evidence=evidence, candidate=snapshot.candidate, task=profile.task,
            context_manifest=contract.repository_work.context_manifest,
            evaluator_profile=DigestReference(reference="urn:long-haul:evaluator:sealed", digest=profile.fingerprint),
            evaluator_environment=DigestReference(reference="urn:long-haul:evaluator:environment",
                                                  digest=profile.environment.fingerprint),
            sandbox_implementation=self.sandbox.identity, contract_fingerprint=work_contract_fingerprint(contract),
            checks=observations, validity="invalid" if invalid else "valid", invalid_reasons=invalid,
        )
        return packet, evaluate_repository_packet(contract, packet, snapshot.candidate, profile)


def repository_evidence_event(packet: RepositoryEvidencePacketV1) -> Event:
    """Append to the existing event history; no separate evidence store."""
    checked = RepositoryEvidencePacketV1.model_validate(packet.model_dump())
    return Event(event_type="work_evidence", source="repository-evaluator-v1", payload={
        "contract_id": checked.evidence.contract_id, "attempt_id": checked.evidence.attempt_id,
        "repository_evidence": checked.model_dump(mode="json"),
    })
