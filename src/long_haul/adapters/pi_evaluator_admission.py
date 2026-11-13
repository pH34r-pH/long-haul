"""Trusted evaluator-profile binding before any worker-visible delivery."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..resource_policy import (
    PolicyDecisionStatus,
    ResourceAction,
    ResourcePolicyEvaluator,
)
from ..work.private_evaluator import PRIVATE_PROFILE_REFERENCE, PrivateEvaluatorProfile
from ..work.repository_evaluation_models import (
    RepositoryEvaluationProfileV1,
    evaluator_implementation_reference,
)
from ..work.repository_evaluator import (
    _validate_requirements,
    public_evaluator_reference,
)


@dataclass(frozen=True)
class EvaluatorWorkerAdmission:
    """Control-plane entry; public policy and profile are not model-supplied."""

    profile: object = field(repr=False)
    policy: object = field(default=None, repr=False)
    expected_policy_fingerprint: str | None = None
    request: object = field(default=None, repr=False)
    expected_request_fingerprint: str | None = None

    def authorize(self, contract, attempt, now):
        extension = contract.repository_work
        profile = self.profile
        if extension is None or extension.sealed_evaluator is None:
            raise PermissionError("worker evaluator admission requires a sealed evaluator reference")
        actual_context = (attempt.contract_id, attempt.mission_id, attempt.repository_id)
        expected_context = (contract.contract_id, contract.mission_id, extension.source.repository_id)
        if actual_context != expected_context:
            raise PermissionError("worker evaluator admission belongs to another attempt context")
        if type(profile) not in {PrivateEvaluatorProfile, RepositoryEvaluationProfileV1}:
            raise PermissionError("worker evaluator profile type is not admitted")
        actual = (extension.task, extension.sealed_evaluator.digest, extension.public_evaluation, profile.implementation)
        expected = (profile.task, profile.fingerprint, public_evaluator_reference(profile), evaluator_implementation_reference())
        if actual != expected:
            raise PermissionError("worker evaluator does not match the exact frozen contract")
        _validate_requirements(contract, profile)
        if type(profile) is PrivateEvaluatorProfile:
            if extension.sealed_evaluator.reference != PRIVATE_PROFILE_REFERENCE:
                raise PermissionError("private worker evaluator requires the keyed profile version")
            if self.policy is not None or self.request is not None:
                raise PermissionError("private evaluator admission cannot claim public profile policy")
        elif type(profile) is RepositoryEvaluationProfileV1:
            if extension.sealed_evaluator.reference != "urn:long-haul:evaluator:sealed":
                raise PermissionError("legacy worker evaluator requires the legacy profile version")
            self._authorize_public(contract, attempt, now)
        else:
            raise PermissionError("worker evaluator profile type is not admitted")

    def _authorize_public(self, contract, attempt, now):
        policy, request = self.policy, self.request
        if policy is None or request is None or now.tzinfo is None or now.utcoffset() is None:
            raise PermissionError("legacy evaluator requires a current public-use admission")
        actual = (policy.fingerprint, request.fingerprint, policy.identity.content_digest,
                  request.caller_id, request.crew_id, request.contract_id, request.mission_id,
                  request.repository_id, request.purpose)
        expected = (self.expected_policy_fingerprint, self.expected_request_fingerprint, self.profile.fingerprint,
                    attempt.session_id, attempt.crew_id, contract.contract_id, contract.mission_id,
                    contract.repository_work.source.repository_id, "public-evaluator-fixture")
        if actual != expected or request.now > now:
            raise PermissionError("legacy evaluator public-use admission is stale or mismatched")
        evaluator = ResourcePolicyEvaluator()
        for action in (ResourceAction.READ, ResourceAction.AI_AUGMENT, ResourceAction.DISTRIBUTE):
            current = request.model_copy(update={"action": action, "now": now})
            if evaluator.evaluate(policy, current).status is not PolicyDecisionStatus.ALLOW:
                raise PermissionError("legacy evaluator is not explicitly admitted for public delivery")
