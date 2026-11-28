"""Export reviewed metadata for an already published DCAT distribution."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from .resource_policy import (
    PolicyDecision,
    PolicyDecisionStatus,
    ResourceAction,
    ResourcePolicyEvaluator,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from .resource_policy_models import canonical_json

DCAT_VERSION = "https://www.w3.org/TR/2024/REC-vocab-dcat-3-20240822/"
DCAT_DISTRIBUTION_PURPOSE = "dcat-distribution-metadata"
PUBLIC_ACCESS = "http://publications.europa.eu/resource/authority/access-right/PUBLIC"

_CONTEXT = {
    "@version": 1.1,
    "dcat": "http://www.w3.org/ns/dcat#",
    "dct": "http://purl.org/dc/terms/",
    "odrl": "http://www.w3.org/ns/odrl/2/",
    "prov": "http://www.w3.org/ns/prov#",
    "spdx": "http://spdx.org/rdf/terms#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
}


def _reviewed_url(value: AnyHttpUrl) -> AnyHttpUrl:
    if value.scheme != "https" or any(part is not None for part in (
        value.username, value.password, value.query, value.fragment,
    )):
        raise ValueError("expected a stable HTTPS reference without credentials, query, or fragment")
    return value


ReviewedUrl = Annotated[AnyHttpUrl, AfterValidator(_reviewed_url)]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class PublishedDistribution(BaseModel):
    """Public facts pinned by an independent review of the publication evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: ReviewedUrl
    distribution_id: ReviewedUrl
    download_url: ReviewedUrl
    title: Label
    version: Label
    issued_at: AwareDatetime
    byte_size: int = Field(strict=True, ge=0)
    sha256: Digest
    media_type: str = Field(pattern=r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
    publication_receipt_url: ReviewedUrl
    publication_receipt_sha256: Digest
    license_url: ReviewedUrl | None = None
    rights_url: ReviewedUrl | None = None
    odrl_policy_url: ReviewedUrl | None = None

    @model_validator(mode="after")
    def validate_identities(self) -> PublishedDistribution:
        if self.dataset_id == self.distribution_id:
            raise ValueError("a dataset and its distribution need distinct identities")
        return self

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        return "sha256:" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _parents(policy: ResourcePolicyProfile) -> set[str]:
    parents = set(policy.derived_from)
    if policy.redacted_from:
        parents.add(policy.redacted_from)
    return parents


def _review_source_policies(
    policy: ResourcePolicyProfile,
    sources: tuple[ResourcePolicyProfile, ...],
    expected: Mapping[str, str],
) -> tuple[ResourcePolicyProfile, ...]:
    root = policy.identity.resource_id
    snapshots = tuple(source.model_copy(deep=True) for source in sources)
    fingerprints = {source.identity.resource_id: source.fingerprint for source in snapshots}
    if len(fingerprints) != len(snapshots) or root in fingerprints or fingerprints != expected:
        raise ValueError("DCAT source policies differ from the reviewed fingerprint set")

    graph = {record.identity.resource_id: _parents(record) for record in (policy, *snapshots)}
    if set().union(*graph.values()) != set(fingerprints):
        raise ValueError("DCAT requires the exact derivation and redaction source closure")
    # Together with the parent-set check, acyclicity gives the distribution as the only root.
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        raise ValueError("DCAT source policy closure contains a cycle") from exc
    return snapshots


def _validate_binding(
    publication: PublishedDistribution, policy: ResourcePolicyProfile, request: ResourceUseRequest,
) -> None:
    identity = policy.identity
    if identity.resource_type != "distribution" or identity.resource_id != str(publication.distribution_id):
        raise ValueError("DCAT requires a policy for the exact published distribution")
    if (identity.version, identity.content_digest, identity.media_type) != (
        publication.version, f"sha256:{publication.sha256}", publication.media_type,
    ):
        raise ValueError("DCAT distribution version, digest, or media type differs from its policy")
    if request.resource_id != identity.resource_id:
        raise ValueError("DCAT request must identify the reviewed distribution")
    if request.action is not ResourceAction.DERIVE or request.purpose != DCAT_DISTRIBUTION_PURPOSE:
        raise ValueError("DCAT requires a derive request for distribution metadata")
    if request.audience_id != "public" or publication.issued_at > request.now:
        raise ValueError("DCAT requires a public audience and a past or current publication time")
    if publication.license_url is not None and str(publication.license_url) not in policy.license_references:
        raise ValueError("DCAT license differs from the reviewed policy")
    if publication.rights_url is not None and str(publication.rights_url) not in policy.rights_references:
        raise ValueError("DCAT rights statement differs from the reviewed policy")
    if publication.odrl_policy_url is not None and str(publication.odrl_policy_url) != policy.policy_id:
        raise ValueError("DCAT ODRL reference differs from the reviewed policy")


def _distribution_node(publication: PublishedDistribution) -> dict[str, Any]:
    node = {
        "@id": str(publication.distribution_id),
        "@type": ["dcat:Distribution", "prov:Entity"],
        "dct:title": publication.title,
        "dct:issued": {"@type": "xsd:dateTime", "@value": publication.issued_at.isoformat()},
        "dct:accessRights": {"@id": PUBLIC_ACCESS},
        "dcat:downloadURL": {"@id": str(publication.download_url)},
        "dcat:mediaType": {"@id": f"https://www.iana.org/assignments/media-types/{publication.media_type}"},
        "dcat:byteSize": {"@type": "xsd:nonNegativeInteger", "@value": str(publication.byte_size)},
        "spdx:checksum": {
            "@type": "spdx:Checksum",
            "spdx:algorithm": {"@id": "spdx:checksumAlgorithm_sha256"},
            "spdx:checksumValue": {"@type": "xsd:hexBinary", "@value": publication.sha256},
        },
        "dct:references": {"@id": str(publication.publication_receipt_url)},
    }
    for field, value in (
        ("dct:license", publication.license_url),
        ("dct:rights", publication.rights_url),
        ("odrl:hasPolicy", publication.odrl_policy_url),
    ):
        if value is not None:
            node[field] = {"@id": str(value)}
    return node


def export_dcat_distribution(
    publication: PublishedDistribution,
    policy: ResourcePolicyProfile,
    request: ResourceUseRequest,
    *,
    expected_publication_fingerprint: str,
    expected_policy_fingerprint: str,
    source_policies: tuple[ResourcePolicyProfile, ...] = (),
    expected_source_fingerprints: Mapping[str, str] | None = None,
) -> tuple[bytes, PolicyDecision]:
    """Return offline JSON-LD and a local decision for the reviewed public projection."""
    publication = PublishedDistribution.model_validate(publication.model_dump())
    policy = policy.model_copy(deep=True)
    request = ResourceUseRequest.model_validate(request.model_dump())
    if publication.fingerprint != expected_publication_fingerprint:
        raise ValueError("DCAT requires the independently reviewed publication fingerprint")
    if policy.fingerprint != expected_policy_fingerprint:
        raise ValueError("DCAT requires the independently reviewed policy fingerprint")
    _validate_binding(publication, policy, request)
    sources = _review_source_policies(policy, source_policies, expected_source_fingerprints or {})
    evaluator = ResourcePolicyEvaluator()
    if sources:
        decision = evaluator.evaluate_all(
            (policy, *sources), lambda resource_id: request.model_copy(update={"resource_id": resource_id}),
        )
    else:
        decision = evaluator.evaluate(policy, request)
    if decision.status is not PolicyDecisionStatus.ALLOW:
        raise ValueError(f"DCAT metadata derivation denied: {decision.status.value}")

    metadata = {
        "@context": dict(_CONTEXT),
        "@id": str(publication.dataset_id),
        "@type": "dcat:Dataset",
        "dcat:version": publication.version,
        "dct:conformsTo": {"@id": DCAT_VERSION},
        "dcat:distribution": _distribution_node(publication),
    }
    return canonical_json(metadata).encode("utf-8") + b"\n", decision
