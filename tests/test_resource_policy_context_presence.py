"""Unknown context must not silently defeat a potentially applicable prohibition."""
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from long_haul.resource_policy import (
    PolicyDecisionStatus,
    PolicyRule,
    ResourceIdentity,
    ResourcePolicyEvaluator,
    ResourcePolicyProfile,
    ResourceUseRequest,
)

NOW = datetime(2026, 11, 12, tzinfo=UTC)


def request(**context):
    return ResourceUseRequest(request_id="fixture", resource_id="urn:fixture:resource", action="ai-augment",
                              purpose="research", caller_id="fixture", now=NOW, **context)


def policy(prohibition, *, allow=True):
    permissions = [PolicyRule(rule_id="allow", actions=["ai-augment"], purposes=["research"])] if allow else []
    return ResourcePolicyProfile(identity=ResourceIdentity(resource_id="urn:fixture:resource", resource_type="dataset"),
                                 policy_id="urn:fixture:policy", policy_assigner="urn:fixture:issuer",
                                 lifecycle="active", permissions=permissions, prohibitions=[prohibition])


@pytest.mark.parametrize(("field", "rule_field"), [
    ("crew_id", "crew_ids"), ("mission_id", "mission_ids"), ("contract_id", "contract_ids"),
    ("repository_id", "repository_ids"), ("audience_id", "audience_ids"),
])
def test_unknown_prohibition_context_cannot_grant_permission(field, rule_field):
    rule = PolicyRule(rule_id="restricted", actions=["ai-augment"], purposes=["research"],
                      **{rule_field: ["restricted"]})
    evaluator = ResourcePolicyEvaluator()
    assert evaluator.evaluate(policy(rule), request()).status is PolicyDecisionStatus.UNKNOWN
    assert evaluator.evaluate(policy(rule), request(**{field: "restricted"})).status is PolicyDecisionStatus.CONFLICT
    assert evaluator.evaluate(policy(rule), request(**{field: "other"})).status is PolicyDecisionStatus.ALLOW
    absent = request(known_absent_context=[field])
    assert evaluator.evaluate(policy(rule), absent).status is PolicyDecisionStatus.ALLOW
    assert absent.fingerprint != request().fingerprint


def test_known_mismatch_excludes_prohibition_even_if_another_dimension_is_unknown():
    rule = PolicyRule(rule_id="restricted", actions=["ai-augment"], purposes=["research"],
                      crew_ids=["restricted"], audience_ids=["public"])
    assert ResourcePolicyEvaluator().evaluate(policy(rule), request(crew_id="other")).status is PolicyDecisionStatus.ALLOW
    assert ResourcePolicyEvaluator().evaluate(policy(rule), request(crew_id="restricted")).status is PolicyDecisionStatus.UNKNOWN


def test_unrelated_prohibition_does_not_block_and_definite_prohibition_still_conflicts():
    unrelated = PolicyRule(rule_id="unrelated", actions=["read"], crew_ids=["restricted"])
    evaluator = ResourcePolicyEvaluator()
    assert evaluator.evaluate(policy(unrelated), request()).status is PolicyDecisionStatus.ALLOW
    unknown = PolicyRule(rule_id="unknown", actions=["ai-augment"], crew_ids=["restricted"])
    definite = PolicyRule(rule_id="definite", actions=["ai-augment"], purposes=["research"])
    combined = policy(unknown).model_copy(update={"prohibitions": [unknown, definite]})
    assert evaluator.evaluate(combined, request()).status is PolicyDecisionStatus.CONFLICT


def test_derived_use_preserves_unknown_source_and_binds_verified_absence():
    rule = PolicyRule(rule_id="restricted", actions=["ai-augment"], purposes=["research"], crew_ids=["restricted"])
    first = policy(rule)
    second = policy(rule).model_copy(update={"identity": ResourceIdentity(resource_id="urn:fixture:other",
                                                                          resource_type="dataset")})
    evaluator = ResourcePolicyEvaluator()

    def evaluate(**context):
        return evaluator.evaluate_all([first, second], lambda resource: request(**context).model_copy(
            update={"resource_id": resource}))

    unknown = evaluate()
    absent = evaluate(known_absent_context=["crew_id"])
    assert unknown.status is PolicyDecisionStatus.UNKNOWN
    assert absent.status is PolicyDecisionStatus.ALLOW
    assert unknown.request_fingerprint != absent.request_fingerprint
    assert len(unknown.source_policy_fingerprints) == 2


def test_absence_claims_are_canonical_and_cannot_contradict_context():
    assert request().fingerprint == "resource-use-v1:7733c5613b05d3616eac818969cd1b4658ca4fba8cddf671eecd51ab167a3dac"
    first = request(known_absent_context=["crew_id", "audience_id"])
    second = request(known_absent_context=["audience_id", "crew_id"])
    assert first.fingerprint == second.fingerprint
    with pytest.raises(ValidationError):
        request(crew_id="present", known_absent_context=["crew_id"])
    with pytest.raises(ValidationError):
        request(known_absent_context=["crew_id", "crew_id"])
    with pytest.raises(ValidationError):
        request(known_absent_context=["caller_id"])
    forged = first.model_copy(update={"crew_id": "present"})
    rule = PolicyRule(rule_id="restricted", actions=["ai-augment"], crew_ids=["present"])
    with pytest.raises(ValidationError):
        ResourcePolicyEvaluator().evaluate(policy(rule), forged)


def test_optional_purpose_requires_verified_absence_for_a_purpose_free_permission():
    source = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:resource", resource_type="dataset"),
        policy_id="urn:fixture:policy", policy_assigner="urn:fixture:issuer", lifecycle="active",
        permissions=[PolicyRule(rule_id="no-purpose", actions=["read"])],
        prohibitions=[PolicyRule(rule_id="no-research", actions=["read"], purposes=["research"])],
    )
    common = {"request_id": "fixture", "resource_id": "urn:fixture:resource", "action": "read",
              "caller_id": "fixture", "now": NOW}
    unknown = ResourceUseRequest(**common)
    none = ResourceUseRequest(**common, known_absent_context=["purpose"])
    research = ResourceUseRequest(**common, purpose="research")
    evaluator = ResourcePolicyEvaluator()
    assert evaluator.evaluate(source, unknown).status is PolicyDecisionStatus.UNKNOWN
    assert evaluator.evaluate(source, none).status is PolicyDecisionStatus.ALLOW
    assert evaluator.evaluate(source, research).status is PolicyDecisionStatus.DENY
    assert unknown.fingerprint != none.fingerprint
    with pytest.raises(ValidationError):
        ResourceUseRequest(**common, purpose="research", known_absent_context=["purpose"])
    with pytest.raises(ValidationError):
        ResourceUseRequest(**common, purpose="  ")
    with pytest.raises(ValidationError):
        request(known_absent_context=["purpose"])


def test_unknown_optional_purpose_does_not_satisfy_a_purpose_free_permission():
    source = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:resource", resource_type="dataset"),
        policy_id="urn:fixture:policy", policy_assigner="urn:fixture:issuer", lifecycle="active",
        permissions=[PolicyRule(rule_id="no-purpose", actions=["read"])],
    )
    requested = ResourceUseRequest(request_id="fixture", resource_id="urn:fixture:resource", action="read",
                                   caller_id="fixture", now=NOW)
    assert ResourcePolicyEvaluator().evaluate(source, requested).status is PolicyDecisionStatus.UNKNOWN


def test_explicit_purpose_selects_only_its_matching_rules():
    source = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:resource", resource_type="dataset"),
        policy_id="urn:fixture:policy", policy_assigner="urn:fixture:issuer", lifecycle="active",
        permissions=[PolicyRule(rule_id="teaching", actions=["read"], purposes=["teaching"])],
        prohibitions=[PolicyRule(rule_id="no-research", actions=["read"], purposes=["research"])],
    )
    common = {"request_id": "fixture", "resource_id": "urn:fixture:resource", "action": "read",
              "caller_id": "fixture", "now": NOW}
    evaluator = ResourcePolicyEvaluator()
    assert evaluator.evaluate(source, ResourceUseRequest(**common)).status is PolicyDecisionStatus.UNKNOWN
    assert evaluator.evaluate(source, ResourceUseRequest(**common, purpose="teaching")).status is PolicyDecisionStatus.ALLOW
    assert evaluator.evaluate(source, ResourceUseRequest(**common, purpose="research")).status is PolicyDecisionStatus.DENY
