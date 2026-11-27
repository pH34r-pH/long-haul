"""Read back an independent evaluation for a retained repository attempt."""

from __future__ import annotations

from .contracts import WorkContract, WorkEvaluation
from .harness import HarnessResult
from .repository_evaluation_models import (
    RepositoryEvaluationProfileV1,
    RepositoryEvidencePacketV1,
    evaluate_repository_packet,
)
from .repository_models import DigestReference, content_digest


def _check_attempt_binding(contract: WorkContract, result: HarnessResult) -> None:
    attempt = result.attempt
    extension = contract.repository_work
    if extension is None or (
        attempt.contract_id != contract.contract_id
        or attempt.mission_id != contract.mission_id
        or attempt.contract_digest
        != content_digest(contract.model_dump_json().encode())
        or attempt.repository_id != extension.source.repository_id
        or attempt.repository_extension_digest != extension.fingerprint
        or attempt.context_manifest != extension.context_manifest
    ):
        raise ValueError(
            "retained attempt does not match the frozen repository contract"
        )


def readback_repository_attempt(
    contract: WorkContract,
    profile: RepositoryEvaluationProfileV1,
    result: HarnessResult,
    packet: RepositoryEvidencePacketV1,
    *,
    expected_result_fingerprint: str,
    expected_packet_fingerprint: str,
) -> WorkEvaluation:
    """Recompute acceptance from independently retained pins.

    Use the semantic trace verifier to obtain a result when reading retained JSONL.
    The evaluator packet remains an independent record; a settled harness carries
    no acceptance decision of its own.
    """
    result = HarnessResult.model_validate(result.model_dump())
    packet = RepositoryEvidencePacketV1.model_validate(packet.model_dump())
    if (
        result.fingerprint != expected_result_fingerprint
        or packet.fingerprint != expected_packet_fingerprint
    ):
        raise ValueError(
            "repository attempt readback differs from the retained evidence pins"
        )
    _check_attempt_binding(contract, result)
    if result.terminal != "settled" or result.candidate_disposition != "frozen":
        raise ValueError(
            "repository evaluation readback requires a settled frozen candidate"
        )
    expected = DigestReference(
        reference="artifact:frozen-worker-candidate",
        digest=packet.candidate.fingerprint,
    )
    if (
        result.candidate != expected
        or packet.evidence.attempt_id != result.attempt.attempt_id
        or packet.evidence.contract_id != contract.contract_id
    ):
        raise ValueError(
            "repository evaluator packet belongs to a different attempt or candidate"
        )
    return evaluate_repository_packet(contract, packet, packet.candidate, profile)
