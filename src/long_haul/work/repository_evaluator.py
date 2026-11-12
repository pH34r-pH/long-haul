"""Independent public/sealed checks against immutable, read-only candidates."""
from __future__ import annotations

import time
from datetime import UTC, datetime

from ..events.store import Event
from ..resource_policy_models import canonical_json
from .contracts import EvidenceState, WorkContract
from .evaluator import CandidateArtifact, CheckRecord, EvaluatorIdentity, EvidencePacket
from .private_evaluator import (
    PRIVATE_PROFILE_REFERENCE,
    PrivateEvaluatorProfile,
    PrivateEvidenceAdmission,
    PrivateEvidenceCheck,
    PrivateEvidenceSummary,
)
from .repository_candidate import CandidateSnapshot
from .repository_evaluation_models import (
    FrozenRepositoryCandidate,
    RepositoryCheckEvidence,
    RepositoryEvaluationProfileV1,
    RepositoryEvidencePacketV1,
    _packet_binding_matches,
    evaluate_repository_packet,
    evaluator_check_fingerprint,
    evaluator_implementation_reference,
    work_contract_fingerprint,
)
from .repository_evidence_admission import PublicEvidenceAdmission
from .repository_models import DigestReference, content_digest
from .repository_sandbox import SandboxAdapter, SandboxResult


def public_evaluator_reference(profile: RepositoryEvaluationProfileV1) -> DigestReference:
    return DigestReference(reference="urn:long-haul:repository-evaluator:public",
                           digest=content_digest(canonical_json(profile.public_projection()).encode()))


def _validate_binding(contract, snapshot, profile, sandbox):
    extension = contract.repository_work
    if extension is None or extension.sealed_evaluator is None:
        raise ValueError("repository evaluator requires an exact evaluator profile reference")
    if isinstance(profile, PrivateEvaluatorProfile) and extension.sealed_evaluator.reference != PRIVATE_PROFILE_REFERENCE:
        raise ValueError("private evaluator requires the versioned private profile reference")
    actual = (extension.task, extension.public_evaluation, extension.sealed_evaluator.digest,
              snapshot.candidate.base, snapshot.candidate.contract_fingerprint,
              sandbox.environment, sandbox.identity, profile.implementation)
    expected = (profile.task, public_evaluator_reference(profile), profile.fingerprint,
                extension.source, work_contract_fingerprint(contract), profile.environment,
                profile.sandbox, evaluator_implementation_reference())
    if actual != expected:
        raise ValueError("repository evaluator task/source/contract/profile/environment binding mismatch")
    _validate_requirements(contract, profile)
    if not any(check.kind == "scope" for check in profile.checks):
        raise ValueError("repository evaluator requires trusted scope evidence")
    commands = sum(check.kind == "command" for check in profile.checks)
    if commands * profile.environment.limits.wall_seconds > contract.budget.wall_seconds:
        raise ValueError("evaluator worst-case wall budget exceeds work contract")


def _validate_requirements(contract, profile):
    predicates = {predicate.predicate_id: predicate for predicate in contract.success_predicates}
    if set(predicates) != {check.requirement.predicate_id for check in profile.checks}:
        raise ValueError("evaluator requirements differ from work contract")
    for check in profile.checks:
        requirement = check.requirement
        description = (profile.public_requirement(check)["description"] if isinstance(profile, PrivateEvaluatorProfile)
                       else requirement.description)
        reference = DigestReference(reference="urn:long-haul:evaluator-requirement:" + requirement.predicate_id,
                                    digest=evaluator_check_fingerprint(profile, check))
        if (predicates[requirement.predicate_id].evaluator_reference != reference
                or predicates[requirement.predicate_id].description != description):
            raise ValueError("repository predicates must use exact evaluator requirement references")


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


def _observe_check(check, snapshot, target, sandbox, scope_valid, profile):
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
        check_fingerprint=evaluator_check_fingerprint(profile, check), state=state, outcome=outcome,
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
        if isinstance(profile, PrivateEvaluatorProfile):
            raise TypeError("private profiles require evaluate_private and an explicit private evidence sink")
        return self._evaluate(contract, snapshot, profile, attempt_id=attempt_id, packet_id=packet_id)

    def _evaluate(self, contract, snapshot, profile, *, attempt_id, packet_id):
        _validate_binding(contract, snapshot, profile, self.sandbox)
        started = time.monotonic()
        scope_valid = all(contract.permits_path(path) for path in snapshot.candidate.changed_paths)
        with snapshot.materialize() as target:
            observations = tuple(_observe_check(check, snapshot, target, self.sandbox, scope_valid, profile)
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
            evaluator_profile=DigestReference(reference=PRIVATE_PROFILE_REFERENCE if isinstance(profile, PrivateEvaluatorProfile)
                                              else "urn:long-haul:evaluator:sealed", digest=profile.fingerprint),
            evaluator_environment=DigestReference(reference="urn:long-haul:evaluator:environment",
                                                  digest=profile.environment.fingerprint),
            sandbox_implementation=self.sandbox.identity, contract_fingerprint=work_contract_fingerprint(contract),
            checks=observations, validity="invalid" if invalid else "valid", invalid_reasons=invalid,
        )
        return packet, evaluate_repository_packet(contract, packet, snapshot.candidate, profile)

    def evaluate_private(self, contract, snapshot, profile, *, attempt_id, packet_id, private_sink):
        """Only authenticated control may register the private evidence sink."""
        if not isinstance(profile, PrivateEvaluatorProfile) or not isinstance(private_sink, PrivateEvidenceAdmission):
            raise TypeError("private evaluation requires a keyed profile and admitted private evidence sink")
        private_sink.authorize(contract, profile, attempt_id, datetime.now(UTC))
        packet, evaluation = self._evaluate(contract, snapshot, profile, attempt_id=attempt_id, packet_id=packet_id)
        private_sink.authorize(contract, profile, attempt_id, datetime.now(UTC))
        private_sink.writer(packet)
        return _private_summary(packet, profile), evaluation


def _private_summary(packet, profile):
    definitions = {check.requirement.predicate_id: check for check in profile.checks}
    checks = tuple(PrivateEvidenceCheck(
        predicate_id=observed.predicate_id, classification=observed.classification,
        check_commitment=observed.check_fingerprint, state=observed.state.value, outcome=observed.outcome,
        observation_commitment=(profile.observation_commitment(definitions[observed.predicate_id], observed)
                                if observed.classification != "PUBLIC" else None),
    ) for observed in packet.checks)
    return PrivateEvidenceSummary(
        contract_id=packet.evidence.contract_id, attempt_id=packet.evidence.attempt_id,
        candidate_fingerprint=profile.binding_commitment("candidate", packet.candidate.fingerprint),
        task_fingerprint=profile.binding_commitment("task", profile.task.digest),
        evaluator_fingerprint=profile.fingerprint,
        contract_fingerprint=profile.binding_commitment("contract", packet.contract_fingerprint),
        environment_fingerprint=profile.binding_commitment("environment", packet.evaluator_environment.digest),
        sandbox_fingerprint=profile.binding_commitment("sandbox", packet.sandbox_implementation.digest),
        checks=checks, validity=packet.validity,
    )


def repository_evidence_event(packet: RepositoryEvidencePacketV1, *, contract=None, candidate=None,
                              profile=None, admission=None) -> Event:
    """Create a V1 public event only for an explicitly admitted exact packet."""
    if isinstance(packet, PrivateEvidenceSummary):
        raise TypeError("private summaries require a verified private evidence event")
    if packet.evaluator_profile.reference == PRIVATE_PROFILE_REFERENCE:
        raise ValueError("private raw evaluator evidence cannot enter the public event stream")
    if (not isinstance(admission, PublicEvidenceAdmission) or type(profile) is not RepositoryEvaluationProfileV1
            or not isinstance(contract, WorkContract) or not isinstance(candidate, FrozenRepositoryCandidate)):
        raise TypeError("legacy evidence requires explicit public disclosure admission")
    checked = RepositoryEvidencePacketV1.model_validate(packet.model_dump())
    if (checked.evaluator_profile.reference != "urn:long-haul:evaluator:sealed"
            or not _packet_binding_matches(contract, checked, candidate, profile)):
        raise ValueError("legacy evidence does not match the exact evaluator binding")
    if checked.validity == "valid":
        evaluate_repository_packet(contract, checked, candidate, profile)
    admission.authorize(checked, contract, profile, datetime.now(UTC))
    return Event(event_type="work_evidence", source="repository-evaluator-v1", payload={
        "contract_id": checked.evidence.contract_id, "attempt_id": checked.evidence.attempt_id,
        "repository_evidence": checked.model_dump(mode="json"),
    })


def private_repository_evidence_event(summary, *, contract, packet, candidate, profile):
    """Verify private evidence against exact inputs before emitting its redacted event."""
    if not isinstance(profile, PrivateEvaluatorProfile) or not isinstance(summary, PrivateEvidenceSummary):
        raise TypeError("private evidence requires a keyed profile and summary")
    if packet.evaluator_profile.reference != PRIVATE_PROFILE_REFERENCE:
        raise ValueError("private evidence packet has the wrong profile")
    packet = RepositoryEvidencePacketV1.model_validate(packet.model_dump())
    if not _packet_binding_matches(contract, packet, candidate, profile) or _private_summary(packet, profile) != summary:
        raise ValueError("private evidence does not match its verified summary")
    if packet.validity == "valid":
        evaluate_repository_packet(contract, packet, candidate, profile)
    return Event(event_type="work_evidence", source="repository-evaluator-private-v2", payload={
        "contract_id": summary.contract_id, "attempt_id": summary.attempt_id,
        "repository_evidence": summary.model_dump(mode="json"),
    })
