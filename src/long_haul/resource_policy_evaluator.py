"""Deterministic evaluation for the Long-Haul resource policy profile."""
from __future__ import annotations

import hashlib
import platform
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pydantic
import pydantic_core

from .resource_policy_models import (
    PolicyDecision,
    PolicyDecisionStatus,
    PolicyDuty,
    PolicyRule,
    ResourceAction,
    ResourceLifecycle,
    ResourcePolicyProfile,
    ResourceUseRequest,
    canonical_json,
)

RequestFactory = Callable[[str], ResourceUseRequest]
DecisionFields = dict[str, Any]


def _implementation_identity():
    root = Path(__file__).parent
    files = ("resource_policy.py", "resource_policy_models.py", "resource_policy_evaluator.py")
    manifest = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
    data = canonical_json({"files": manifest, "python": platform.python_version(),
                           "pydantic": pydantic.__version__, "pydantic_core": pydantic_core.__version__}).encode()
    return "resource-policy-evaluator-v1:sha256:" + hashlib.sha256(data).hexdigest()


_IMPORTED_IMPLEMENTATION = _implementation_identity()


def _verified_implementation():
    current = _implementation_identity()
    if current != _IMPORTED_IMPLEMENTATION:
        raise ValueError("policy evaluator implementation changed after import")
    return current


def _decision_fields(
    profile: ResourcePolicyProfile,
    request: ResourceUseRequest,
) -> DecisionFields:
    return {
        "request_id": request.request_id,
        "request_fingerprint": request.fingerprint,
        "resource_id": request.resource_id,
        "policy_id": profile.policy_id,
        "policy_fingerprint": profile.fingerprint,
        "evaluated_at": request.now,
        "evaluator_implementation": _verified_implementation(),
    }


def _rule_matches_context(rule: PolicyRule, request: ResourceUseRequest) -> bool:
    dimensions = (
        (rule.principal_ids, request.caller_id),
        (rule.crew_ids, request.crew_id),
        (rule.mission_ids, request.mission_id),
        (rule.contract_ids, request.contract_id),
        (rule.repository_ids, request.repository_id),
        (rule.audience_ids, request.audience_id),
    )
    return all(not allowed or actual in allowed for allowed, actual in dimensions)


def _rule_matches_time(rule: PolicyRule, request: ResourceUseRequest) -> bool:
    starts_in_time = not rule.valid_from or request.now >= rule.valid_from
    ends_in_time = not rule.valid_until or request.now <= rule.valid_until
    return starts_in_time and ends_in_time


def _rule_matches(rule: PolicyRule, request: ResourceUseRequest, *, prohibition: bool = False) -> bool:
    if request.action not in rule.actions:
        return False
    if rule.purposes and request.purpose not in rule.purposes:
        return False
    if (not prohibition and not rule.purposes
            and (request.purpose is not None or "purpose" not in request.known_absent_context)):
        return False
    return _rule_matches_context(rule, request) and _rule_matches_time(rule, request)


def _lifecycle_status(lifecycle: ResourceLifecycle) -> PolicyDecisionStatus | None:
    return {
        ResourceLifecycle.RETRACTED: PolicyDecisionStatus.RETRACTED,
        ResourceLifecycle.EXPIRED: PolicyDecisionStatus.EXPIRED,
        ResourceLifecycle.DELETED: PolicyDecisionStatus.DENY,
        ResourceLifecycle.UNAVAILABLE: PolicyDecisionStatus.UNAVAILABLE,
        ResourceLifecycle.UNKNOWN: PolicyDecisionStatus.UNKNOWN,
        ResourceLifecycle.STALE: PolicyDecisionStatus.REQUIRE_REVIEW,
        ResourceLifecycle.SUPERSEDED: PolicyDecisionStatus.REQUIRE_REVIEW,
    }.get(lifecycle)


def _unfulfilled_duties(
    permissions: Iterable[PolicyRule],
    request: ResourceUseRequest,
) -> list[PolicyDuty]:
    fulfilled = set(request.fulfilled_duties)
    return [
        duty
        for rule in permissions
        for duty in rule.duties
        if duty.duty_id not in fulfilled
    ]


def _duty_status(duties: Iterable[PolicyDuty]) -> PolicyDecisionStatus:
    return (
        PolicyDecisionStatus.REQUIRE_CONSENT
        if any(duty.requires_consent for duty in duties)
        else PolicyDecisionStatus.REQUIRE_REVIEW
    )


def _derived_resource_id(decisions: Iterable[PolicyDecision]) -> str:
    resources = sorted(decision.resource_id for decision in decisions)
    digest = hashlib.sha256(canonical_json(resources).encode()).hexdigest()
    return f"derived:{digest}"


def _flatten(decisions: Iterable[PolicyDecision], field_name: str) -> list[str]:
    return sorted({
        value
        for decision in decisions
        for value in getattr(decision, field_name)
    })


def _privacy_restriction(profile: ResourcePolicyProfile, request: ResourceUseRequest) -> str | None:
    if request.action is ResourceAction.DELETE:
        return None  # Cleanup still requires an explicit matching permission.
    if profile.invalidated_at and request.now >= profile.invalidated_at:
        return "resource provenance has been invalidated"
    if profile.privacy.deletion_required:
        return "resource is pending required deletion"
    if profile.privacy.purposes and request.purpose not in profile.privacy.purposes:
        return "requested purpose is incompatible with resource privacy policy"
    return None


def _preflight_decision(
    profile: ResourcePolicyProfile,
    request: ResourceUseRequest,
    common: DecisionFields,
) -> PolicyDecision | None:
    if request.resource_id != profile.identity.resource_id:
        return PolicyDecision(
            status=PolicyDecisionStatus.DENY,
            reason="resource identity mismatch",
            **common,
        )
    lifecycle_status = _lifecycle_status(profile.lifecycle)
    if lifecycle_status is not None:
        return PolicyDecision(
            status=lifecycle_status,
            reason=f"resource lifecycle is {profile.lifecycle.value}",
            **common,
        )
    restriction = _privacy_restriction(profile, request)
    if restriction:
        return PolicyDecision(status=PolicyDecisionStatus.DENY, reason=restriction, **common)
    retention_until = profile.privacy.retention_until
    if request.action is not ResourceAction.DELETE and retention_until and request.now >= retention_until:
        return PolicyDecision(
            status=PolicyDecisionStatus.EXPIRED,
            reason="resource retention period expired",
            **common,
        )
    return None


def _matching_rules(
    profile: ResourcePolicyProfile,
    request: ResourceUseRequest,
) -> tuple[list[PolicyRule], list[PolicyRule]]:
    permissions = [rule for rule in profile.permissions if _rule_matches(rule, request)]
    prohibitions = [rule for rule in profile.prohibitions if _rule_matches(rule, request, prohibition=True)]
    return permissions, prohibitions


def _unresolved_prohibition(rule: PolicyRule, request: ResourceUseRequest) -> bool:
    if request.action not in rule.actions or not _rule_matches_time(rule, request):
        return False
    unknown = False
    if rule.purposes:
        if request.purpose is None:
            if "purpose" in request.known_absent_context:
                return False
            unknown = True
        elif request.purpose not in rule.purposes:
            return False
    dimensions = ((rule.principal_ids, "caller_id"), (rule.crew_ids, "crew_id"),
                  (rule.mission_ids, "mission_id"), (rule.contract_ids, "contract_id"),
                  (rule.repository_ids, "repository_id"), (rule.audience_ids, "audience_id"))
    for allowed, field in dimensions:
        if not allowed:
            continue
        actual = getattr(request, field)
        if actual is None:
            if field in request.known_absent_context:
                return False
            unknown = True
        elif actual not in allowed:
            return False
    return unknown


def _resolve_matches(
    permissions: list[PolicyRule],
    prohibitions: list[PolicyRule],
    request: ResourceUseRequest,
    common: DecisionFields,
) -> PolicyDecision:
    permission_ids = [rule.rule_id for rule in permissions]
    prohibition_ids = [rule.rule_id for rule in prohibitions]
    if permissions and prohibitions:
        return PolicyDecision(
            status=PolicyDecisionStatus.CONFLICT,
            reason="matching permission and prohibition require reviewed resolution",
            matched_permissions=permission_ids,
            matched_prohibitions=prohibition_ids,
            **common,
        )
    if prohibitions:
        return PolicyDecision(
            status=PolicyDecisionStatus.DENY,
            reason="an explicit prohibition matched",
            matched_prohibitions=prohibition_ids,
            **common,
        )
    if not permissions:
        return PolicyDecision(
            status=PolicyDecisionStatus.UNKNOWN,
            reason="no explicit permission matched; fail closed",
            **common,
        )
    duties = _unfulfilled_duties(permissions, request)
    if duties:
        return PolicyDecision(
            status=_duty_status(duties),
            reason="permission matched but required duties remain unsatisfied",
            matched_permissions=permission_ids,
            unsatisfied_duties=[duty.duty_id for duty in duties],
            **common,
        )
    return PolicyDecision(
        status=PolicyDecisionStatus.ALLOW,
        reason="explicit permission matched and all duties are satisfied",
        matched_permissions=permission_ids,
        **common,
    )


def _blocked_decisions(decisions: list[PolicyDecision]) -> list[PolicyDecision] | None:
    precedence = (
        PolicyDecisionStatus.DENY,
        PolicyDecisionStatus.RETRACTED,
        PolicyDecisionStatus.EXPIRED,
        PolicyDecisionStatus.CONFLICT,
        PolicyDecisionStatus.UNAVAILABLE,
        PolicyDecisionStatus.REQUIRE_CONSENT,
        PolicyDecisionStatus.REQUIRE_REVIEW,
        PolicyDecisionStatus.UNKNOWN,
    )
    for status in precedence:
        blocked = [decision for decision in decisions if decision.status is status]
        if blocked:
            return blocked
    return None


def _derived_decision(
    decisions: list[PolicyDecision],
    blocked: list[PolicyDecision] | None,
) -> PolicyDecision:
    implementations = {decision.evaluator_implementation for decision in decisions}
    if len(implementations) != 1 or None in implementations:
        raise ValueError("derived decisions require one exact evaluator implementation")
    resource_id = _derived_resource_id(decisions)
    first = min(blocked or decisions, key=lambda item: item.resource_id)
    sources = {item.resource_id: item.policy_fingerprint for item in decisions}
    fingerprint = hashlib.sha256(canonical_json(sources).encode()).hexdigest()
    bindings = sorted(item.request_fingerprint for item in decisions)
    return PolicyDecision(
        request_id=first.request_id,
        request_fingerprint="derived-use-v1:" + hashlib.sha256(canonical_json(bindings).encode()).hexdigest(),
        resource_id=resource_id,
        status=first.status if blocked else PolicyDecisionStatus.ALLOW,
        reason=(f"derived use blocked by {len(blocked)} source policy decision(s)" if blocked
                else "all source policies explicitly allow the derived use"),
        matched_permissions=_flatten(decisions, "matched_permissions"),
        matched_prohibitions=_flatten(decisions, "matched_prohibitions"),
        unsatisfied_duties=_flatten(decisions, "unsatisfied_duties"),
        policy_fingerprint="derived-policy-v1:" + fingerprint,
        source_policy_fingerprints=sources,
        evaluated_at=first.evaluated_at,
        evaluator_implementation=first.evaluator_implementation,
    )


class ResourcePolicyEvaluator:
    """Evaluate resource-side policy; callers still enforce the result."""

    @staticmethod
    def implementation_reference() -> str:
        """Identify observed source and runtime versions; this is not attestation."""
        return _verified_implementation()

    def evaluate(
        self,
        profile: ResourcePolicyProfile,
        request: ResourceUseRequest,
    ) -> PolicyDecision:
        request = ResourceUseRequest.model_validate(request.model_dump())
        common = _decision_fields(profile, request)
        preflight = _preflight_decision(profile, request, common)
        if preflight is not None:
            return preflight
        permissions, prohibitions = _matching_rules(profile, request)
        unresolved = [rule.rule_id for rule in profile.prohibitions if _unresolved_prohibition(rule, request)]
        if unresolved and not prohibitions:
            return PolicyDecision(
                status=PolicyDecisionStatus.UNKNOWN,
                reason="prohibition context is unknown: " + ", ".join(sorted(unresolved)),
                matched_permissions=[rule.rule_id for rule in permissions],
                **common,
            )
        return _resolve_matches(permissions, prohibitions, request, common)

    def evaluate_all(
        self,
        profiles: Iterable[ResourcePolicyProfile],
        request_factory: RequestFactory,
    ) -> PolicyDecision:
        records = list(profiles)
        requests = [request_factory(profile.identity.resource_id) for profile in records]
        bindings = {canonical_json(request.model_dump(mode="json", exclude={"resource_id"}))
                    for request in requests}
        if len(bindings) > 1:
            raise ValueError("all source requests must describe the same use")
        if len({record.identity.resource_id for record in records}) != len(records):
            raise ValueError("derived source resource ids must be unique")
        decisions = [self.evaluate(profile, request) for profile, request in zip(records, requests)]
        if not decisions:
            raise ValueError("at least one source policy is required")
        return _derived_decision(decisions, _blocked_decisions(decisions))
