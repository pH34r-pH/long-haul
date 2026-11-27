"""Trusted, exact candidate-input policy admission for repository evaluation.

The control plane must authenticate this registry and attest that each file's
parents cover its complete source and derived lineage. Candidate contents cannot
construct or expand the registry. This module does not authenticate the caller,
prove lineage completeness, or qualify the sandbox or host.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..resource_policy import (
    PolicyDecisionStatus,
    ResourceAction,
    ResourcePolicyEvaluator,
    ResourcePolicyProfile,
    ResourceUseRequest,
)

MAX_EVALUATION_LINEAGE_ENTRIES = 4096


@dataclass(frozen=True)
class EvaluationInputPolicy:
    """A caller-admitted policy and exact original evaluation request."""

    profile: ResourcePolicyProfile = field(repr=False)
    expected_policy_fingerprint: str = field(repr=False)
    request: ResourceUseRequest = field(repr=False)
    expected_request_fingerprint: str = field(repr=False)


@dataclass(frozen=True)
class EvaluationFileAdmission:
    """The first policy identifies exact file bytes; remaining parents narrow use."""

    path: str = field(repr=False)
    content_digest: str = field(repr=False)
    parents: tuple[EvaluationInputPolicy, ...] = field(repr=False)


@dataclass(frozen=True)
class CandidateInputAdmission:
    """Control-plane attestation of every file and its complete policy lineage."""

    candidate_fingerprint: str = field(repr=False)
    files: tuple[EvaluationFileAdmission, ...] = field(repr=False)

    def authorize(self, snapshot, contract, evaluator, attempt_id: str, now: datetime) -> None:
        extension = contract.repository_work
        if (extension is None or not isinstance(attempt_id, str) or not attempt_id
                or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None
                or self.candidate_fingerprint != snapshot.candidate.fingerprint
                or type(self.files) is not tuple):
            raise PermissionError("candidate input admission lacks exact current binding")
        admitted, candidate_files = _validate_coverage(self.files, snapshot)
        admitted_resources = {}
        for path, entry in admitted.items():
            _validate_file(entry, candidate_files[path])
            seen = set()
            for parent in entry.parents:
                _authorize_parent(parent, seen, admitted_resources, contract, evaluator, attempt_id, now)


def _validate_coverage(files, snapshot):
    admitted = {}
    for entry in files:
        if type(entry) is not EvaluationFileAdmission or entry.path in admitted:
            raise PermissionError("candidate input admission is incomplete or ambiguous")
        admitted[entry.path] = entry
    candidate_files = {entry.path: entry for entry in snapshot.candidate.files}
    if len(candidate_files) != len(snapshot.candidate.files) or set(admitted) != set(candidate_files):
        raise PermissionError("candidate input admission is incomplete or ambiguous")
    if any(type(entry.parents) is not tuple for entry in admitted.values()) or sum(
        len(entry.parents) for entry in admitted.values()
    ) > MAX_EVALUATION_LINEAGE_ENTRIES:
        raise PermissionError("candidate input admission exceeds lineage limits")
    return admitted, candidate_files


def _validate_file(entry, candidate_file):
    if (entry.content_digest != candidate_file.digest or not entry.parents
            or type(entry.parents[0]) is not EvaluationInputPolicy
            or not isinstance(entry.parents[0].profile, ResourcePolicyProfile)
            or entry.parents[0].profile.identity.content_digest != entry.content_digest):
        raise PermissionError("candidate input admission lacks exact content or lineage")


def _authorize_parent(parent, seen, admitted_resources, contract, evaluator, attempt_id, now):
    if type(parent) is not EvaluationInputPolicy:
        raise PermissionError("candidate input admission lacks exact content or lineage")
    policy, request = parent.profile, parent.request
    if not isinstance(policy, ResourcePolicyProfile) or not isinstance(request, ResourceUseRequest):
        raise PermissionError("candidate input admission lacks current policy")
    actual = (policy.fingerprint, request.fingerprint, request.resource_id, request.request_id,
              request.action, request.purpose, request.caller_id, request.contract_id,
              request.mission_id, request.repository_id)
    expected = (parent.expected_policy_fingerprint, parent.expected_request_fingerprint,
                policy.identity.resource_id, "repository-evaluation:" + attempt_id,
                ResourceAction.EVALUATE, "repository-evaluation", evaluator.evaluator_id,
                contract.contract_id, contract.mission_id, contract.repository_work.source.repository_id)
    if actual != expected or request.action is not ResourceAction.EVALUATE or request.now > now:
        raise PermissionError("candidate input admission lacks current policy")
    if policy.identity.resource_id in seen:
        raise PermissionError("candidate input admission has duplicate lineage")
    seen.add(policy.identity.resource_id)
    previous = admitted_resources.get(policy.identity.resource_id)
    if previous is not None and previous != parent.expected_policy_fingerprint:
        raise PermissionError("candidate input admission has conflicting policy identities")
    admitted_resources[policy.identity.resource_id] = parent.expected_policy_fingerprint
    for action in (ResourceAction.READ, ResourceAction.EVALUATE):
        current = request.model_copy(update={"action": action, "now": now})
        if ResourcePolicyEvaluator().evaluate(policy, current).status is not PolicyDecisionStatus.ALLOW:
            raise PermissionError("candidate input admission denies evaluation use")
