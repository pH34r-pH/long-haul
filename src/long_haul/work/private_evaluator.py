"""Private per-check commitments for future versioned evaluator integration.

This object is control-plane-only. Private check definitions and keys must not
be sent to a worker or serialized into a public artifact.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import Field, model_validator

from ..resource_policy import (
    PolicyDecisionStatus,
    ResourceAction,
    ResourcePolicyEvaluator,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from ..resource_policy_models import canonical_json
from .repository_evaluation_models import RepositoryEvaluationProfileV1
from .repository_models import FrozenProjection, content_digest

PRIVATE_PROFILE_REFERENCE = "urn:long-haul:evaluator:private-v2"


@dataclass(frozen=True)
class PrivateEvidenceAdmission:
    """Trusted control registry entry; never construct it from model or tool data.

    The registered writer is trusted to persist raw evidence privately. The
    resource policy authorizes retention, not the writer's implementation.
    """

    policy: ResourcePolicyProfile = field(repr=False)
    expected_policy_fingerprint: str
    request: ResourceUseRequest = field(repr=False)
    expected_request_fingerprint: str
    contract_fingerprint: str
    evaluator_fingerprint: str
    attempt_id: str
    writer: Callable = field(repr=False)

    def authorize(self, contract, profile, attempt_id, now):
        from .repository_evaluation_models import work_contract_fingerprint

        request = self.request
        extension = contract.repository_work
        if now.tzinfo is None or now.utcoffset() is None or extension is None or not callable(self.writer):
            raise PermissionError("private evidence lacks exact current retention admission")
        actual = (self.attempt_id, self.policy.fingerprint, request.fingerprint, self.contract_fingerprint,
                  self.evaluator_fingerprint, self.policy.identity.content_digest, request.request_id,
                  request.action, request.purpose, request.caller_id, request.contract_id, request.mission_id,
                  request.repository_id)
        expected = (attempt_id, self.expected_policy_fingerprint, self.expected_request_fingerprint,
                    work_contract_fingerprint(contract), profile.fingerprint, profile.fingerprint,
                    "private-evidence:" + attempt_id, ResourceAction.RETAIN, "repository-evaluator-evidence",
                    profile.evaluator_id, contract.contract_id, contract.mission_id, extension.source.repository_id)
        if actual != expected or request.action is not ResourceAction.RETAIN or request.now > now:
            raise PermissionError("private evidence lacks exact current retention admission")
        fresh = request.model_copy(update={"now": now})
        if ResourcePolicyEvaluator().evaluate(self.policy, fresh).status is not PolicyDecisionStatus.ALLOW:
            raise PermissionError("private evidence retention policy is not currently allowed")


class PrivateEvidenceCheck(FrozenProjection):
    predicate_id: str = Field(min_length=1, max_length=128)
    classification: Literal["PUBLIC", "SEALED", "POLICY", "INSPECTION"]
    check_commitment: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    state: Literal["PASS", "FAIL", "UNKNOWN"]
    outcome: Literal["pass", "fail", "invalid", "timeout", "output_limit", "resource_exhausted"]
    observation_commitment: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def nonpublic_has_commitment(self):
        if (self.classification != "PUBLIC") != (self.observation_commitment is not None):
            raise ValueError("only non-public observations carry keyed commitments")
        return self


class PrivateEvidenceSummary(FrozenProjection):
    """Public-safe summary; detailed private observations stay with the caller's sink."""

    schema_version: Literal[2] = 2
    profile: Literal["repository-evidence-private-v2"] = "repository-evidence-private-v2"
    contract_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    candidate_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluator_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    contract_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    environment_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sandbox_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    checks: tuple[PrivateEvidenceCheck, ...] = Field(min_length=1, max_length=128)
    validity: Literal["valid", "invalid"]

    @model_validator(mode="after")
    def unique_checks(self):
        if len({check.predicate_id for check in self.checks}) != len(self.checks):
            raise ValueError("private evidence predicate ids must be unique")
        return self


class PrivateEvaluatorProfile:
    """Bind an exact private profile with separately held per-non-public-check keys."""

    __slots__ = ("_keys", "_profile")

    def __init__(self, profile, keys):
        if not isinstance(profile, RepositoryEvaluationProfileV1):
            raise TypeError("expected a validated repository evaluator profile")
        expected = {check.requirement.predicate_id for check in profile.checks
                    if check.requirement.classification != "PUBLIC"}
        if not expected or set(keys) != expected:
            raise ValueError("one private key is required for each non-public check")
        if any(not isinstance(key, bytes) or not 32 <= len(key) <= 64 for key in keys.values()):
            raise ValueError("non-public check keys must contain 32 to 64 bytes")
        if len(set(keys.values())) != len(keys):
            raise ValueError("non-public checks require independent keys")
        self._profile = profile
        self._keys = dict(keys)

    @classmethod
    def from_profile(cls, profile, keys):
        return cls(profile, keys)

    @classmethod
    def generate(cls, profile):
        """Generate fresh keys; trusted control must persist them securely for replay."""
        keys = {check.requirement.predicate_id: secrets.token_bytes(32) for check in profile.checks
                if check.requirement.classification != "PUBLIC"}
        return cls(profile, keys)

    def __repr__(self):
        return "PrivateEvaluatorProfile(<private>)"

    @property
    def checks(self):
        return self._profile.checks

    @property
    def task(self):
        return self._profile.task

    @property
    def environment(self):
        return self._profile.environment

    @property
    def implementation(self):
        return self._profile.implementation

    @property
    def sandbox(self):
        return self._profile.sandbox

    @property
    def evaluator_id(self):
        return self._profile.evaluator_id

    @property
    def evaluator_version(self):
        return self._profile.evaluator_version

    def check_fingerprint(self, check):
        if check not in self._profile.checks:
            raise ValueError("check does not belong to the frozen evaluator")
        if check.requirement.classification == "PUBLIC":
            return check.fingerprint
        payload = canonical_json(check.model_dump(mode="json")).encode()
        digest = hmac.new(self._keys[check.requirement.predicate_id], b"long-haul-private-check-v2\0" + payload,
                          hashlib.sha256).hexdigest()
        return "sha256:" + digest

    def public_projection(self):
        result = self._profile.public_projection()
        result["profile"] = "repository-evaluation-public-v2"
        result["requirements"] = [self.public_requirement(check) for check in self._profile.checks]
        return result

    def public_requirement(self, check):
        if check not in self._profile.checks:
            raise ValueError("check does not belong to the frozen evaluator")
        requirement = check.requirement.model_dump(mode="json")
        if check.requirement.classification != "PUBLIC":
            requirement["description"] = "Non-public evaluator requirement"
        return requirement

    def observation_commitment(self, check, observation):
        if check.requirement.classification == "PUBLIC" or check not in self._profile.checks:
            raise ValueError("only an admitted non-public check has a private observation commitment")
        payload = canonical_json(observation.model_dump(mode="json")).encode()
        digest = hmac.new(self._keys[check.requirement.predicate_id],
                          b"long-haul-private-observation-v2\0" + payload, hashlib.sha256).hexdigest()
        return "sha256:" + digest

    @property
    def fingerprint(self):
        value = self._profile.model_dump(mode="json", exclude={"checks"})
        value["profile"] = "repository-evaluation-private-v2"
        value["schema_version"] = 2
        value["checks"] = [self.check_fingerprint(check) for check in self._profile.checks]
        return content_digest(canonical_json(value).encode())
