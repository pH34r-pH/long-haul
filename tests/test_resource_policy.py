from datetime import UTC, datetime, timedelta

import pytest

from long_haul.resource_policy import (
    AuthorityClass,
    PolicyDecisionStatus,
    PolicyDuty,
    PolicyRule,
    PrivacyProfile,
    ResourceAction,
    ResourceIdentity,
    ResourceLifecycle,
    ResourcePolicyEvaluator,
    ResourcePolicyProfile,
    ResourceUseRequest,
)

NOW = datetime(2026, 11, 11, tzinfo=UTC)


def profile(*, permissions=None, prohibitions=None, lifecycle=ResourceLifecycle.ACTIVE):
    return ResourcePolicyProfile(
        identity=ResourceIdentity(
            resource_id="urn:test:memory:1",
            resource_type="memory",
            title="One memory",
            content_digest="sha256:abc",
        ),
        authority_class=AuthorityClass.ACCEPTED_MEMORY,
        lifecycle=lifecycle,
        policy_id="urn:policy:1",
        policy_assigner="urn:agent:reviewer",
        permissions=permissions or [],
        prohibitions=prohibitions or [],
    )


def request(action=ResourceAction.AI_AUGMENT, **kwargs):
    return ResourceUseRequest(
        request_id="request-1",
        resource_id="urn:test:memory:1",
        action=action,
        purpose=kwargs.pop(
            "purpose",
            "mission-assistance" if action is ResourceAction.AI_AUGMENT else None,
        ),
        caller_id="session-1",
        crew_id="crew-1",
        mission_id="mission-1",
        now=NOW,
        **kwargs,
    )


def test_explicit_permission_allows_scoped_ai_augmentation():
    policy = profile(
        permissions=[
            PolicyRule(
                rule_id="allow-augment",
                actions=[ResourceAction.AI_AUGMENT],
                purposes=["mission-assistance"],
                crew_ids=["crew-1"],
                mission_ids=["mission-1"],
            )
        ]
    )
    decision = ResourcePolicyEvaluator().evaluate(policy, request())
    assert decision.status is PolicyDecisionStatus.ALLOW
    assert decision.matched_permissions == ["allow-augment"]


def test_index_permission_does_not_imply_ai_augmentation():
    policy = profile(
        permissions=[PolicyRule(rule_id="index", actions=[ResourceAction.INDEX])]
    )
    assert (
        ResourcePolicyEvaluator().evaluate(policy, request()).status
        is PolicyDecisionStatus.UNKNOWN
    )


def test_prohibition_and_permission_conflict_is_explicit():
    allow = PolicyRule(
        rule_id="allow",
        actions=[ResourceAction.AI_TRAINING],
        purposes=["research-training"],
    )
    deny = PolicyRule(
        rule_id="deny",
        actions=[ResourceAction.AI_TRAINING],
        purposes=["research-training"],
    )
    policy = profile(permissions=[allow], prohibitions=[deny])
    training = request(ResourceAction.AI_TRAINING, purpose="research-training")
    assert (
        ResourcePolicyEvaluator().evaluate(policy, training).status
        is PolicyDecisionStatus.CONFLICT
    )


def test_unfulfilled_consent_duty_requires_consent():
    duty = PolicyDuty(
        duty_id="consent-1",
        action="obtain-consent",
        description="obtain current user consent",
        requires_consent=True,
    )
    policy = profile(
        permissions=[
            PolicyRule(
                rule_id="allow-with-consent",
                actions=[ResourceAction.AI_AUGMENT],
                purposes=["mission-assistance"],
                duties=[duty],
            )
        ]
    )
    evaluator = ResourcePolicyEvaluator()
    assert evaluator.evaluate(policy, request()).status is PolicyDecisionStatus.REQUIRE_CONSENT
    allowed = request(fulfilled_duties=["consent-1"])
    assert evaluator.evaluate(policy, allowed).status is PolicyDecisionStatus.ALLOW


def test_stale_retracted_and_retention_expiry_fail_closed():
    evaluator = ResourcePolicyEvaluator()
    assert (
        evaluator.evaluate(
            profile(lifecycle=ResourceLifecycle.STALE), request()
        ).status
        is PolicyDecisionStatus.REQUIRE_REVIEW
    )
    assert (
        evaluator.evaluate(
            profile(lifecycle=ResourceLifecycle.RETRACTED), request()
        ).status
        is PolicyDecisionStatus.RETRACTED
    )
    policy = profile(
        permissions=[PolicyRule(rule_id="read", actions=[ResourceAction.READ])]
    )
    policy.privacy = PrivacyProfile(retention_until=NOW - timedelta(seconds=1))
    assert (
        evaluator.evaluate(policy, request(ResourceAction.READ)).status
        is PolicyDecisionStatus.EXPIRED
    )


def test_missing_policy_is_unknown_not_implicit_allow():
    assert (
        ResourcePolicyEvaluator().evaluate(profile(), request()).status
        is PolicyDecisionStatus.UNKNOWN
    )


def test_profile_fingerprint_and_jsonld_are_deterministic():
    policy = profile(
        permissions=[
            PolicyRule(
                rule_id="read",
                actions=[ResourceAction.READ],
                principal_ids=["session-1"],
            )
        ]
    )
    assert policy.fingerprint == policy.model_copy(deep=True).fingerprint
    document = policy.to_jsonld()
    assert document["lh:fingerprint"] == policy.fingerprint
    assert document["id"] == "urn:test:memory:1"
    assert "odrl" in document["@context"]


def test_derived_use_requires_every_source_to_allow():
    evaluator = ResourcePolicyEvaluator()
    allow = profile(
        permissions=[
            PolicyRule(
                rule_id="allow",
                actions=[ResourceAction.AI_TRAINING],
                purposes=["research-training"],
            )
        ]
    )
    deny = profile(
        prohibitions=[
            PolicyRule(
                rule_id="deny",
                actions=[ResourceAction.AI_TRAINING],
                purposes=["research-training"],
            )
        ]
    )
    deny.identity.resource_id = "urn:test:memory:2"

    def factory(resource_id):
        return ResourceUseRequest(
            request_id="derived-request",
            resource_id=resource_id,
            action=ResourceAction.AI_TRAINING,
            purpose="research-training",
            caller_id="session-1",
            now=NOW,
        )

    decision = evaluator.evaluate_all([allow, deny], factory)
    assert decision.status is PolicyDecisionStatus.DENY


def test_consequential_action_requires_explicit_purpose():
    with pytest.raises(ValueError):
        ResourceUseRequest(
            request_id="r",
            resource_id="urn:x",
            action=ResourceAction.AI_TRAINING,
            caller_id="session",
            now=NOW,
        )


def test_purpose_free_prohibition_applies_to_every_purpose():
    policy = profile(
        permissions=[PolicyRule(rule_id="scoped", actions=[ResourceAction.AI_AUGMENT],
                                purposes=["mission-assistance"])],
        prohibitions=[PolicyRule(rule_id="all-purposes", actions=[ResourceAction.AI_AUGMENT])],
    )
    assert ResourcePolicyEvaluator().evaluate(policy, request()).status is PolicyDecisionStatus.CONFLICT


@pytest.mark.parametrize("restriction", ["purpose", "deletion", "invalidation", "retention-boundary"])
def test_privacy_and_invalidation_cannot_be_overridden_by_permission(restriction):
    policy = profile(permissions=[PolicyRule(
        rule_id="allow", actions=[ResourceAction.AI_AUGMENT], purposes=["mission-assistance"],
    )])
    if restriction == "purpose":
        policy.privacy.purposes = ["different-purpose"]
    elif restriction == "deletion":
        policy.privacy.deletion_required = True
    elif restriction == "invalidation":
        policy.invalidated_at = NOW
    else:
        policy.privacy.retention_until = NOW
    assert ResourcePolicyEvaluator().evaluate(policy, request()).status is not PolicyDecisionStatus.ALLOW


def test_expired_resource_can_be_deleted_only_with_explicit_permission():
    policy = profile(permissions=[PolicyRule(rule_id="cleanup", actions=[ResourceAction.DELETE])])
    policy.privacy.retention_until = NOW
    policy.privacy.deletion_required = True
    policy.invalidated_at = NOW
    evaluator = ResourcePolicyEvaluator()
    assert evaluator.evaluate(policy, request(ResourceAction.DELETE)).status is PolicyDecisionStatus.UNKNOWN
    assert evaluator.evaluate(policy, request(ResourceAction.DELETE, known_absent_context=["purpose"])).status is PolicyDecisionStatus.ALLOW
    policy.permissions = []
    assert evaluator.evaluate(policy, request(ResourceAction.DELETE, known_absent_context=["purpose"])).status is not PolicyDecisionStatus.ALLOW


def test_derived_use_rejects_mixed_requests_and_binds_source_policies():
    first = profile(permissions=[PolicyRule(rule_id="read", actions=[ResourceAction.READ])])
    second = first.model_copy(deep=True)
    second.identity.resource_id = "urn:test:memory:2"

    def factory(resource_id):
        return request(ResourceAction.READ).model_copy(update={"resource_id": resource_id})

    evaluator = ResourcePolicyEvaluator()
    result = evaluator.evaluate_all([first, second], factory)
    second.policy_id = "urn:policy:changed"
    changed = evaluator.evaluate_all([first, second], factory)
    assert result.policy_fingerprint != changed.policy_fingerprint
    assert evaluator.evaluate_all([second, first], factory) == changed

    def mixed(resource_id):
        return factory(resource_id).model_copy(update={"caller_id": resource_id})

    with pytest.raises(ValueError, match="same use"):
        evaluator.evaluate_all([first, second], mixed)
