"""Exact-packet public disclosure admission from trusted control."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..resource_policy import (
    PolicyDecisionStatus,
    ResourceAction,
    ResourcePolicyEvaluator,
)
from ..resource_policy_models import canonical_json
from .repository_models import content_digest


def evidence_packet_digest(packet):
    """Keep the raw packet digest in trusted admission, not in public metadata."""
    return content_digest(canonical_json(packet.model_dump(mode="json")).encode())


@dataclass(frozen=True)
class PublicEvidenceAdmission:
    """Control registry entry for an entire explicitly public packet."""

    policy: object = field(repr=False)
    expected_policy_fingerprint: str
    request: object = field(repr=False)
    expected_request_fingerprint: str

    def authorize(self, packet, contract, profile, now):
        request = self.request
        extension = contract.repository_work
        if extension is None or now.tzinfo is None or now.utcoffset() is None:
            raise PermissionError("public evidence requires exact current disclosure admission")
        actual = (self.policy.fingerprint, request.fingerprint, self.policy.identity.content_digest,
                  request.request_id, request.action, request.purpose, request.caller_id,
                  request.contract_id, request.mission_id, request.repository_id, packet.evidence.contract_id)
        expected = (self.expected_policy_fingerprint, self.expected_request_fingerprint, evidence_packet_digest(packet),
                    "public-evidence:" + packet.evidence.attempt_id, ResourceAction.DISTRIBUTE,
                    "public-evaluator-evidence", profile.evaluator_id, contract.contract_id, contract.mission_id,
                    extension.source.repository_id, contract.contract_id)
        if actual != expected or request.action is not ResourceAction.DISTRIBUTE or request.now > now:
            raise PermissionError("public evidence requires exact current disclosure admission")
        fresh = request.model_copy(update={"now": now})
        if ResourcePolicyEvaluator().evaluate(self.policy, fresh).status is not PolicyDecisionStatus.ALLOW:
            raise PermissionError("public evidence distribution is not currently allowed")
