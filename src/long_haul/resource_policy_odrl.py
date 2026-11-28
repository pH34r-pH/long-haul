"""Native ODRL rule projection for the Long-Haul resource policy profile."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from .resource_policy import (
    PolicyDuty,
    PolicyRule,
    ResourceAction,
    ResourcePolicyProfile,
)
from .resource_policy_models import canonical_json

NATIVE_ODRL_PROFILE = "https://ph34r.dev/ns/long-haul/odrl/v1"
CONTEXT = {
    "odrl": "http://www.w3.org/ns/odrl/2/",
    "lh": "https://ph34r.dev/ns/long-haul/odrl/v1/",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}
SCOPE_OPERANDS = {
    "purposes": "lh:purpose",
    "crew_ids": "lh:crew",
    "mission_ids": "lh:mission",
    "contract_ids": "lh:contract",
    "repository_ids": "lh:repository",
    "audience_ids": "lh:audience",
}
_PROFILE_IRI = "urn:long-haul:odrl:"


def _ref(value: str) -> dict[str, str]:
    return {"@id": value}


def _identifier(kind: str, value: str) -> str:
    return f"{_PROFILE_IRI}{kind}:{quote(value, safe='')}"


def _datetime(value: datetime) -> dict[str, str]:
    return {"@type": "xsd:dateTime", "@value": value.astimezone(UTC).isoformat().replace("+00:00", "Z")}


def _constraint(operand: str, operator: str, value: str | dict[str, str]) -> dict[str, Any]:
    return {
        "@type": "odrl:Constraint",
        "odrl:leftOperand": _ref(operand),
        "odrl:operator": _ref(f"odrl:{operator}"),
        "odrl:rightOperand": value,
    }


def _membership(operand: str, values: list[str]) -> dict[str, Any]:
    choices = [_constraint(operand, "eq", value) for value in sorted(set(values))]
    if len(choices) == 1:
        return choices[0]
    return {"@type": "odrl:LogicalConstraint", "odrl:or": {"@list": choices}}


def _rule_conditions(rule: PolicyRule, fingerprint: str, permission: bool) -> list[dict[str, Any]]:
    conditions = []
    for field, operand in SCOPE_OPERANDS.items():
        values = getattr(rule, field)
        if values:
            conditions.append(_membership(operand, values))
    for instant, operator in ((rule.valid_from, "gteq"), (rule.valid_until, "lteq")):
        if instant is not None:
            conditions.append(_constraint("odrl:dateTime", operator, _datetime(instant)))
    if permission:
        conditions.append(_constraint("lh:admittedPolicy", "eq", fingerprint))
        if not rule.purposes:
            conditions.append(_constraint("lh:purposeState", "eq", "known-absent"))
    return conditions


def _duty(duty: PolicyDuty) -> dict[str, Any]:
    return {
        "@type": "odrl:Duty",
        "odrl:action": _ref(_identifier("duty-action", duty.action)),
        "lh:dutyId": duty.duty_id,
        "lh:description": duty.description,
        "lh:requiresConsent": duty.requires_consent,
        "lh:requiresReview": duty.requires_review,
    }


def _atomic_rules(
    rule: PolicyRule, target: str, assigner: str, fingerprint: str, permission: bool,
) -> list[dict[str, Any]]:
    conditions = _rule_conditions(rule, fingerprint, permission)
    common: dict[str, Any] = {
        "@type": "odrl:Permission" if permission else "odrl:Prohibition",
        "odrl:target": _ref(target),
        "odrl:assigner": _ref(assigner),
        "lh:ruleId": rule.rule_id,
    }
    if conditions:
        common["odrl:constraint"] = conditions
    if rule.source_policy_reference is not None:
        common["lh:sourcePolicyReference"] = rule.source_policy_reference
    if rule.duties:
        duty_property = "odrl:duty" if permission else "lh:declaredProhibitionDuty"
        common[duty_property] = [_duty(duty) for duty in rule.duties]

    atoms = []
    for action in rule.actions:
        for principal in sorted(set(rule.principal_ids)) or (None,):
            atom = {**common, "odrl:action": _ref(_identifier("action", action.value))}
            if principal is not None:
                atom["odrl:assignee"] = _ref(_identifier("party", principal))
            atoms.append(atom)
    return atoms


def _resource_node(profile: ResourcePolicyProfile, target: str) -> dict[str, Any]:
    resource = {
        "@id": target,
        "@type": "odrl:Asset",
        "lh:resourceId": profile.identity.resource_id,
        "lh:lifecycle": profile.lifecycle.value,
        "lh:deletionRequired": profile.privacy.deletion_required,
    }
    if profile.identity.content_digest is not None:
        resource["lh:contentDigest"] = profile.identity.content_digest
    if profile.privacy.purposes:
        resource["lh:privacyPurpose"] = sorted(set(profile.privacy.purposes))
    if profile.derived_from:
        resource["lh:derivedFrom"] = [_ref(_identifier("asset", source)) for source in profile.derived_from]
    if profile.redacted_from is not None:
        resource["lh:redactedFrom"] = _ref(_identifier("asset", profile.redacted_from))
    for field, instant in (("lh:invalidatedAt", profile.invalidated_at),
                           ("lh:retentionUntil", profile.privacy.retention_until)):
        if instant is not None:
            resource[field] = _datetime(instant)
    return resource


def _actors_and_actions(profile: ResourcePolicyProfile) -> list[dict[str, Any]]:
    groups = [*profile.permissions, *profile.prohibitions]
    actors = {profile.policy_assigner, *(principal for rule in groups for principal in rule.principal_ids)}
    parties = [{"@id": _identifier("party", actor), "@type": "odrl:Party", "lh:principalId": actor}
               for actor in sorted(actors)]
    actions = [
        {"@id": _identifier("action", action.value), "@type": "odrl:Action",
         "odrl:includedIn": _ref("odrl:use"), "lh:actionName": action.value}
        for action in ResourceAction
    ]
    duties = sorted({duty.action for rule in groups for duty in rule.duties})
    duty_actions = [
        {"@id": _identifier("duty-action", action), "@type": "odrl:Action",
         "odrl:includedIn": _ref("odrl:use"), "lh:actionName": action}
        for action in duties
    ]
    return [*parties, *actions, *duty_actions]


def export_native_odrl(profile: ResourcePolicyProfile, *, expected_policy_fingerprint: str) -> bytes:
    """Project rules against an independently reviewed resource policy pin."""
    profile = ResourcePolicyProfile.model_validate(profile.model_dump())
    fingerprint = profile.fingerprint
    if fingerprint != expected_policy_fingerprint:
        raise ValueError("native ODRL requires the independently reviewed policy fingerprint")
    if not (profile.permissions or profile.prohibitions):
        raise ValueError("native ODRL Set requires at least one rule")
    target = _identifier("asset", profile.identity.resource_id)
    assigner = _identifier("party", profile.policy_assigner)
    policy: dict[str, Any] = {
        "@id": _identifier("policy", fingerprint),
        "@type": "odrl:Set",
        "odrl:profile": _ref(NATIVE_ODRL_PROFILE),
        "odrl:conflict": _ref("odrl:invalid"),
        "lh:policyId": profile.policy_id,
        "lh:sourceFingerprint": fingerprint,
    }
    for rules, property_name, permission in (
        (profile.permissions, "odrl:permission", True),
        (profile.prohibitions, "odrl:prohibition", False),
    ):
        if rules:
            policy[property_name] = [
                atom for rule in rules
                for atom in _atomic_rules(rule, target, assigner, fingerprint, permission)
            ]
    operands = [{"@id": operand, "@type": "odrl:LeftOperand"}
                for operand in (*SCOPE_OPERANDS.values(), "lh:purposeState", "lh:admittedPolicy")]
    document = {
        "@context": CONTEXT,
        "@graph": [policy, _resource_node(profile, target), *_actors_and_actions(profile), *operands],
    }
    return canonical_json(document).encode("utf-8") + b"\n"


def verify_native_odrl(
    claimed: bytes, profile: ResourcePolicyProfile, *, expected_policy_fingerprint: str,
) -> None:
    """Confirm exact native projection bytes without resolving external JSON-LD contexts."""
    expected = export_native_odrl(profile, expected_policy_fingerprint=expected_policy_fingerprint)
    if claimed != expected:
        raise ValueError("native ODRL graph differs from the reviewed policy projection")
