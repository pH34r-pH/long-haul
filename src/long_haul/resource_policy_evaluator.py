"""Deterministic evaluation for the Long-Haul resource policy profile."""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from typing import Any

from .resource_policy_models import (
    PolicyDecision,
    PolicyDecisionStatus,
    PolicyDuty,
    PolicyRule,
    ResourceLifecycle,
    ResourcePolicyProfile,
    ResourceUseRequest,
    canonical_json,
)

RequestFactory = Callable[[str], ResourceUseRequest]


def _decision_fields(
    profile: ResourcePolicyProfile,
    request: ResourceUseRequest,
) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "resource_id": request.resource_id,
        "policy_id": profile.policy_id,
        "policy_fingerprint": profile.fingerprint,
        "evaluated_at": request.now,
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


def _rule_matches(rule: PolicyRule, request: ResourceUseRequest) -> bool:
    if request.action not in rule.actions:
        return False
    if rule.purposes and request.purpose not in rule.purposes:
        return False
    if not rule.purposes and request.purpose is not None:
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
    return [
        value
        for decision in decisions
        for value in getattr(decision, field_name)
    ]


class ResourcePolicyEvaluator:
    """Evaluate resource-side policy; callers still enforce the result."""

    def evaluate(
        self,
        profile: ResourcePolicyProfile,
        request: ResourceUseRequest,
    ) -> PolicyDecision:
        common = _decision_fields(profile, request)
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
        if profile.privacy.retention_until and request.now > profile.privacy.retention_until:
            return PolicyDecision(
                status=PolicyDecisionStatus.EXPIRED,
                reason="resource retention period expired",
                **common,
            )

        prohibitions = [rule for rule in profile.prohibitions if _rule_matches(rule, request)]
        permissions = [rule for rule in profile.permissions if _rule_matches(rule, request)]
        prohibition_ids = [rule.rule_id for rule in prohibitions]
        permission_ids = [rule.rule_id for rule in permissions]

        if prohibitions and permissions:
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

    def evaluate_all(
        self,
        profiles: Iterable[ResourcePolicyProfile],
        request_factory: RequestFactory,
    ) -> PolicyDecision:
        decisions = [
            self.evaluate(profile, request_factory(profile.identity.resource_id))
            for profile in profiles
        ]
        if not decisions:
            raise ValueError("at least one source policy is required")

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
        blocked = next(
            (
                [decision for decision in decisions if decision.status is status]
                for status in precedence
                if any(decision.status is status for decision in decisions)
            ),
            None,
        )
        resource_id = _derived_resource_id(decisions)
        if blocked:
            first = blocked[0]
            return PolicyDecision(
                request_id=first.request_id,
                resource_id=resource_id,
                status=first.status,
                reason=f"derived use blocked by {len(blocked)} source policy decision(s)",
                matched_permissions=_flatten(decisions, "matched_permissions"),
                matched_prohibitions=_flatten(decisions, "matched_prohibitions"),
                unsatisfied_duties=_flatten(decisions, "unsatisfied_duties"),
                evaluated_at=first.evaluated_at,
            )

        first = decisions[0]
        return PolicyDecision(
            request_id=first.request_id,
            resource_id=resource_id,
            status=PolicyDecisionStatus.ALLOW,
            reason="all source policies explicitly allow the derived use",
            matched_permissions=_flatten(decisions, "matched_permissions"),
            evaluated_at=first.evaluated_at,
        )
