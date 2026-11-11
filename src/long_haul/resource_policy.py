"""Standards-profiled resource policy models and deterministic evaluation.

The canonical exchange form is JSON-LD. Runtime callers may use the Pydantic
projection directly; no RDF store is required. ODRL-like rules describe asset
use, while Long Haul's authenticated policy layer remains authoritative for
execution.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

RESOURCE_POLICY_SCHEMA_VERSION = 1
RESOURCE_POLICY_PROFILE_ID = "https://ph34r.dev/ns/long-haul/resource-policy/v1"

STANDARDS = {
    "prof": "https://www.w3.org/TR/dx-prof/",
    "odrl": "https://www.w3.org/TR/odrl-model/",
    "odrl-ai": "https://w3c.github.io/odrl/ai-vocab/",
    "prov": "https://www.w3.org/TR/prov-o/",
    "dcat": "https://www.w3.org/TR/vocab-dcat-3/",
    "dqv": "https://www.w3.org/TR/vocab-dqv/",
    "dpv": "https://w3id.org/dpv/2.0",
    "croissant": "https://docs.mlcommons.org/croissant/docs/croissant-spec-1.1.html",
    "spdx": "https://spdx.github.io/spdx-spec/v3.0.1/",
    "in-toto": "https://in-toto.io/docs/specs/",
}

JSONLD_CONTEXT: dict[str, Any] = {
    "@version": 1.1,
    "dct": "http://purl.org/dc/terms/",
    "dcat": "http://www.w3.org/ns/dcat#",
    "dqv": "http://www.w3.org/ns/dqv#",
    "odrl": "http://www.w3.org/ns/odrl/2/",
    "prov": "http://www.w3.org/ns/prov#",
    "prof": "http://www.w3.org/ns/dx/prof/",
    "spdx": "https://spdx.org/rdf/3.0.1/terms/",
    "dpv": "https://w3id.org/dpv#",
    "lh": "https://ph34r.dev/ns/long-haul/",
    "id": "@id",
    "type": "@type",
    "title": "dct:title",
    "version": "dct:hasVersion",
    "conformsTo": {"@id": "dct:conformsTo", "@type": "@id"},
    "license": {"@id": "dct:license", "@type": "@id"},
    "rights": {"@id": "dct:rights", "@type": "@id"},
    "hasPolicy": {"@id": "odrl:hasPolicy", "@type": "@id"},
    "derivedFrom": {"@id": "prov:wasDerivedFrom", "@type": "@id"},
    "generatedBy": {"@id": "prov:wasGeneratedBy", "@type": "@id"},
    "invalidatedAt": {
        "@id": "prov:invalidatedAtTime",
        "@type": "http://www.w3.org/2001/XMLSchema#dateTime",
    },
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class AuthorityClass(str, Enum):
    AUTHORITATIVE_CONTROL = "authoritative-control"
    REVIEWED_TASK = "reviewed-task"
    ACCEPTED_MEMORY = "accepted-memory"
    REPOSITORY_REFERENCE = "repository-reference"
    EXTERNAL_SOURCE = "external-source"
    GENERATED_CONTENT = "generated-content"
    EVALUATOR_EVIDENCE = "evaluator-evidence"
    UNKNOWN = "unknown"


class ResourceLifecycle(str, Enum):
    ACTIVE = "active"
    STALE = "stale"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    EXPIRED = "expired"
    DELETED = "deleted"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class ResourceAction(str, Enum):
    READ = "read"
    INDEX = "index"
    SEARCH = "search"
    AI_AUGMENT = "ai-augment"
    EVALUATE = "evaluate"
    DATA_MINING = "data-mining"
    AI_TRAINING = "ai-training"
    GENERATIVE_AI_TRAINING = "generative-ai-training"
    DERIVE = "derive"
    ANNOTATE = "annotate"
    DISTRIBUTE = "distribute"
    PUBLISH = "publish"
    RETAIN = "retain"
    DELETE = "delete"


class PolicyDecisionStatus(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_CONSENT = "REQUIRE_CONSENT"
    REQUIRE_REVIEW = "REQUIRE_REVIEW"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"
    EXPIRED = "EXPIRED"
    RETRACTED = "RETRACTED"
    UNAVAILABLE = "UNAVAILABLE"


class ResourceIdentity(BaseModel):
    resource_id: str = Field(min_length=1)
    resource_type: str = Field(min_length=1)
    title: str | None = None
    version: str | None = None
    content_digest: str | None = None
    media_type: str | None = None
    conforms_to: list[str] = Field(default_factory=lambda: [RESOURCE_POLICY_PROFILE_ID])

    @field_validator("content_digest")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is not None and ":" not in value:
            raise ValueError("content_digest must include an algorithm prefix")
        return value


class ProvenanceActivity(BaseModel):
    activity_id: str = Field(min_length=1)
    activity_type: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    used: list[str] = Field(default_factory=list)
    generated: list[str] = Field(default_factory=list)
    software_reference: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @model_validator(mode="after")
    def validate_times(self) -> "ProvenanceActivity":
        _aware(self.started_at, "started_at")
        _aware(self.ended_at, "ended_at")
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValueError("ended_at cannot precede started_at")
        return self


class PrivacyProfile(BaseModel):
    data_categories: list[str] = Field(default_factory=list)
    data_subjects: list[str] = Field(default_factory=list)
    purposes: list[str] = Field(default_factory=list)
    processing_operations: list[str] = Field(default_factory=list)
    legal_basis: list[str] = Field(default_factory=list)
    consent_references: list[str] = Field(default_factory=list)
    recipients: list[str] = Field(default_factory=list)
    storage_locations: list[str] = Field(default_factory=list)
    retention_until: datetime | None = None
    deletion_required: bool = False
    risk_references: list[str] = Field(default_factory=list)
    measure_references: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_retention(self) -> "PrivacyProfile":
        _aware(self.retention_until, "retention_until")
        return self


class QualityMeasurement(BaseModel):
    metric: str = Field(min_length=1)
    value: float | str | bool
    unit: str | None = None
    evaluator_reference: str = Field(min_length=1)
    measured_at: datetime | None = None

    @model_validator(mode="after")
    def validate_measured_at(self) -> "QualityMeasurement":
        _aware(self.measured_at, "measured_at")
        return self


class PolicyDuty(BaseModel):
    duty_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    description: str = Field(min_length=1)
    requires_consent: bool = False
    requires_review: bool = False


class PolicyRule(BaseModel):
    rule_id: str = Field(min_length=1)
    actions: list[ResourceAction] = Field(min_length=1)
    purposes: list[str] = Field(default_factory=list)
    principal_ids: list[str] = Field(default_factory=list)
    crew_ids: list[str] = Field(default_factory=list)
    mission_ids: list[str] = Field(default_factory=list)
    contract_ids: list[str] = Field(default_factory=list)
    repository_ids: list[str] = Field(default_factory=list)
    audience_ids: list[str] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    duties: list[PolicyDuty] = Field(default_factory=list)
    source_policy_reference: str | None = None

    @model_validator(mode="after")
    def validate_rule(self) -> "PolicyRule":
        _aware(self.valid_from, "valid_from")
        _aware(self.valid_until, "valid_until")
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise ValueError("valid_until cannot precede valid_from")
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("actions must be unique within a rule")
        if len({d.duty_id for d in self.duties}) != len(self.duties):
            raise ValueError("duty ids must be unique within a rule")
        return self


class ResourcePolicyProfile(BaseModel):
    identity: ResourceIdentity
    authority_class: AuthorityClass = AuthorityClass.UNKNOWN
    lifecycle: ResourceLifecycle = ResourceLifecycle.UNKNOWN
    policy_id: str = Field(min_length=1)
    policy_assigner: str = Field(min_length=1)
    permissions: list[PolicyRule] = Field(default_factory=list)
    prohibitions: list[PolicyRule] = Field(default_factory=list)
    provenance: list[ProvenanceActivity] = Field(default_factory=list)
    privacy: PrivacyProfile = Field(default_factory=PrivacyProfile)
    quality: list[QualityMeasurement] = Field(default_factory=list)
    license_references: list[str] = Field(default_factory=list)
    rights_references: list[str] = Field(default_factory=list)
    spdx_references: list[str] = Field(default_factory=list)
    attestation_references: list[str] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)
    redacted_from: str | None = None
    superseded_by: str | None = None
    invalidated_at: datetime | None = None
    schema_version: Literal[1] = RESOURCE_POLICY_SCHEMA_VERSION
    profile_id: Literal[RESOURCE_POLICY_PROFILE_ID] = RESOURCE_POLICY_PROFILE_ID

    @model_validator(mode="after")
    def validate_profile(self) -> "ResourcePolicyProfile":
        _aware(self.invalidated_at, "invalidated_at")
        rule_ids = [r.rule_id for r in [*self.permissions, *self.prohibitions]]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("rule ids must be unique across permissions and prohibitions")
        if self.lifecycle is ResourceLifecycle.SUPERSEDED and not self.superseded_by:
            raise ValueError("superseded resources require superseded_by")
        if self.redacted_from and self.redacted_from == self.identity.resource_id:
            raise ValueError("a redacted derivative must have its own resource identity")
        return self

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        return "resource-policy-v1:" + hashlib.sha256(
            _canonical_json(payload).encode()
        ).hexdigest()

    def to_jsonld(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        return {
            "@context": JSONLD_CONTEXT,
            "id": self.identity.resource_id,
            "type": [
                "dcat:Resource",
                "odrl:Asset",
                "prov:Entity",
                "lh:GovernedResource",
            ],
            "title": self.identity.title,
            "version": self.identity.version,
            "conformsTo": self.identity.conforms_to,
            "lh:schemaVersion": self.schema_version,
            "lh:profileId": self.profile_id,
            "lh:contentDigest": self.identity.content_digest,
            "lh:resourceType": self.identity.resource_type,
            "lh:authorityClass": self.authority_class.value,
            "lh:lifecycle": self.lifecycle.value,
            "hasPolicy": self.policy_id,
            "lh:policyAssigner": self.policy_assigner,
            "lh:permissions": payload["permissions"],
            "lh:prohibitions": payload["prohibitions"],
            "lh:privacy": payload["privacy"],
            "lh:quality": payload["quality"],
            "lh:provenance": payload["provenance"],
            "license": self.license_references,
            "rights": self.rights_references,
            "lh:spdx": self.spdx_references,
            "lh:attestations": self.attestation_references,
            "derivedFrom": self.derived_from,
            "lh:redactedFrom": self.redacted_from,
            "lh:supersededBy": self.superseded_by,
            "invalidatedAt": (
                self.invalidated_at.isoformat() if self.invalidated_at else None
            ),
            "lh:fingerprint": self.fingerprint,
        }


class ResourceUseRequest(BaseModel):
    request_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    action: ResourceAction
    purpose: str | None = None
    caller_id: str = Field(min_length=1)
    crew_id: str | None = None
    mission_id: str | None = None
    contract_id: str | None = None
    repository_id: str | None = None
    audience_id: str | None = None
    fulfilled_duties: list[str] = Field(default_factory=list)
    now: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_now(self) -> "ResourceUseRequest":
        _aware(self.now, "now")
        consequential = {
            ResourceAction.AI_AUGMENT,
            ResourceAction.EVALUATE,
            ResourceAction.DATA_MINING,
            ResourceAction.AI_TRAINING,
            ResourceAction.GENERATIVE_AI_TRAINING,
            ResourceAction.DERIVE,
            ResourceAction.ANNOTATE,
            ResourceAction.DISTRIBUTE,
            ResourceAction.PUBLISH,
        }
        if self.action in consequential and not self.purpose:
            raise ValueError(f"purpose is required for {self.action.value}")
        return self


class PolicyDecision(BaseModel):
    request_id: str
    resource_id: str
    status: PolicyDecisionStatus
    reason: str
    matched_permissions: list[str] = Field(default_factory=list)
    matched_prohibitions: list[str] = Field(default_factory=list)
    unsatisfied_duties: list[str] = Field(default_factory=list)
    policy_id: str | None = None
    policy_fingerprint: str | None = None
    evaluated_at: datetime


class ResourcePolicyEvaluator:
    """Deterministic profile evaluator; callers still enforce the decision."""

    def _rule_matches(self, rule: PolicyRule, request: ResourceUseRequest) -> bool:
        if request.action not in rule.actions:
            return False
        if rule.purposes and request.purpose not in rule.purposes:
            return False
        if not rule.purposes and request.purpose is not None:
            return False
        dimensions = (
            (rule.principal_ids, request.caller_id),
            (rule.crew_ids, request.crew_id),
            (rule.mission_ids, request.mission_id),
            (rule.contract_ids, request.contract_id),
            (rule.repository_ids, request.repository_id),
            (rule.audience_ids, request.audience_id),
        )
        for allowed, actual in dimensions:
            if allowed and actual not in allowed:
                return False
        if rule.valid_from and request.now < rule.valid_from:
            return False
        if rule.valid_until and request.now > rule.valid_until:
            return False
        return True

    def evaluate(
        self,
        profile: ResourcePolicyProfile,
        request: ResourceUseRequest,
    ) -> PolicyDecision:
        common = {
            "request_id": request.request_id,
            "resource_id": request.resource_id,
            "policy_id": profile.policy_id,
            "policy_fingerprint": profile.fingerprint,
            "evaluated_at": request.now,
        }
        if request.resource_id != profile.identity.resource_id:
            return PolicyDecision(
                status=PolicyDecisionStatus.DENY,
                reason="resource identity mismatch",
                **common,
            )

        lifecycle_status = {
            ResourceLifecycle.RETRACTED: PolicyDecisionStatus.RETRACTED,
            ResourceLifecycle.EXPIRED: PolicyDecisionStatus.EXPIRED,
            ResourceLifecycle.DELETED: PolicyDecisionStatus.DENY,
            ResourceLifecycle.UNAVAILABLE: PolicyDecisionStatus.UNAVAILABLE,
            ResourceLifecycle.UNKNOWN: PolicyDecisionStatus.UNKNOWN,
            ResourceLifecycle.STALE: PolicyDecisionStatus.REQUIRE_REVIEW,
            ResourceLifecycle.SUPERSEDED: PolicyDecisionStatus.REQUIRE_REVIEW,
        }.get(profile.lifecycle)
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

        prohibitions = [
            rule for rule in profile.prohibitions if self._rule_matches(rule, request)
        ]
        permissions = [
            rule for rule in profile.permissions if self._rule_matches(rule, request)
        ]
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

        duties = [
            duty
            for rule in permissions
            for duty in rule.duties
            if duty.duty_id not in request.fulfilled_duties
        ]
        if duties:
            status = (
                PolicyDecisionStatus.REQUIRE_CONSENT
                if any(duty.requires_consent for duty in duties)
                else PolicyDecisionStatus.REQUIRE_REVIEW
            )
            return PolicyDecision(
                status=status,
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
        request_factory: Any,
    ) -> PolicyDecision:
        """Fail-closed evaluation for a resource derived from multiple inputs.

        ``request_factory`` receives each resource id and returns a request with the
        same intended action/context. All relevant sources must allow the use.
        """
        decisions = [
            self.evaluate(profile, request_factory(profile.identity.resource_id))
            for profile in profiles
        ]
        if not decisions:
            raise ValueError("at least one source policy is required")
        precedence = [
            PolicyDecisionStatus.DENY,
            PolicyDecisionStatus.RETRACTED,
            PolicyDecisionStatus.EXPIRED,
            PolicyDecisionStatus.CONFLICT,
            PolicyDecisionStatus.UNAVAILABLE,
            PolicyDecisionStatus.REQUIRE_CONSENT,
            PolicyDecisionStatus.REQUIRE_REVIEW,
            PolicyDecisionStatus.UNKNOWN,
        ]
        resource_id = "derived:" + hashlib.sha256(
            _canonical_json(sorted(decision.resource_id for decision in decisions)).encode()
        ).hexdigest()
        for status in precedence:
            failures = [decision for decision in decisions if decision.status is status]
            if failures:
                return PolicyDecision(
                    request_id=failures[0].request_id,
                    resource_id=resource_id,
                    status=status,
                    reason=(
                        "derived use blocked by "
                        f"{len(failures)} source policy decision(s)"
                    ),
                    matched_permissions=[
                        rule
                        for decision in decisions
                        for rule in decision.matched_permissions
                    ],
                    matched_prohibitions=[
                        rule
                        for decision in decisions
                        for rule in decision.matched_prohibitions
                    ],
                    unsatisfied_duties=[
                        duty
                        for decision in decisions
                        for duty in decision.unsatisfied_duties
                    ],
                    evaluated_at=failures[0].evaluated_at,
                )
        first = decisions[0]
        return PolicyDecision(
            request_id=first.request_id,
            resource_id=resource_id,
            status=PolicyDecisionStatus.ALLOW,
            reason="all source policies explicitly allow the derived use",
            matched_permissions=[
                rule for decision in decisions for rule in decision.matched_permissions
            ],
            evaluated_at=first.evaluated_at,
        )


def profile_json_schema() -> dict[str, Any]:
    """Return the authoring projection schema used by YAML/JSON clients."""
    return ResourcePolicyProfile.model_json_schema()
