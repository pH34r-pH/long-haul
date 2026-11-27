"""Project an admitted published dataset snapshot to Croissant 1.1."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import date
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
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

CROISSANT_VERSION = "http://mlcommons.org/croissant/1.1"
CROISSANT_PURPOSE = "croissant-metadata"

# Inline Croissant 1.1 context keeps JSON-LD expansion offline.
CROISSANT_CONTEXT = {
    "@language": None,
    "@vocab": "https://schema.org/",
    "arrayShape": "cr:arrayShape",
    "citeAs": "cr:citeAs",
    "column": "cr:column",
    "conformsTo": "dct:conformsTo",
    "containedIn": "cr:containedIn",
    "cr": "http://mlcommons.org/croissant/",
    "data": {
        "@id": "cr:data",
        "@type": "@json"
    },
    "dataType": {
        "@id": "cr:dataType",
        "@type": "@vocab"
    },
    "dct": "http://purl.org/dc/terms/",
    "description": {
        "@container": "@language"
    },
    "equivalentProperty": "cr:equivalentProperty",
    "examples": {
        "@id": "cr:examples",
        "@type": "@json"
    },
    "extract": "cr:extract",
    "field": "cr:field",
    "fileObject": "cr:fileObject",
    "fileProperty": "cr:fileProperty",
    "fileSet": "cr:fileSet",
    "format": "cr:format",
    "includes": "cr:includes",
    "isArray": "cr:isArray",
    "isLiveDataset": "cr:isLiveDataset",
    "jsonPath": "cr:jsonPath",
    "key": "cr:key",
    "md5": "cr:md5",
    "name": {
        "@container": "@language"
    },
    "parentField": "cr:parentField",
    "path": "cr:path",
    "rai": "http://mlcommons.org/croissant/RAI/",
    "recordSet": "cr:recordSet",
    "references": "cr:references",
    "regex": "cr:regex",
    "repeated": "cr:repeated",
    "replace": "cr:replace",
    "samplingRate": "cr:samplingRate",
    "sc": "https://schema.org/",
    "separator": "cr:separator",
    "source": "cr:source",
    "subField": "cr:subField",
    "transform": "cr:transform"
}

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _reviewed_url(value: AnyHttpUrl) -> AnyHttpUrl:
    if value.scheme != "https" or any(part is not None for part in (
        value.username, value.password, value.query, value.fragment,
    )):
        raise ValueError("Croissant requires an HTTPS reference without credentials, query, or fragment")
    return value


ReviewedUrl = Annotated[AnyHttpUrl, AfterValidator(_reviewed_url)]
LICENSE_URLS = TypeAdapter(tuple[ReviewedUrl, ...])


class DatasetCreator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["Organization", "Person"]
    name: Text


class DatasetFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    content_url: ReviewedUrl
    media_type: str = Field(pattern=r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    def to_file_object(self) -> dict[str, str]:
        return {
            "@id": f"files/{self.name}",
            "@type": "cr:FileObject",
            "name": self.name,
            "contentUrl": str(self.content_url),
            "encodingFormat": self.media_type,
            "sha256": self.sha256,
        }


class DatasetRelease(BaseModel):
    """Reviewed descriptive facts and inventory for one immutable dataset edition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: ReviewedUrl
    description: Text
    date_published: date
    citation: Text | None = None
    creators: tuple[DatasetCreator, ...] = Field(min_length=1)
    files: tuple[DatasetFile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_inventory(self) -> DatasetRelease:
        if len({file.name for file in self.files}) != len(self.files):
            raise ValueError("Croissant file names must be unique")
        return self

    @property
    def fingerprint(self) -> str:
        payload = canonical_json(self.model_dump(mode="json")).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()


def _snapshot_metadata(release: DatasetRelease, policy: ResourcePolicyProfile) -> dict[str, Any]:
    identity = policy.identity
    licenses = LICENSE_URLS.validate_python(policy.license_references)
    if not licenses:
        raise ValueError("Croissant requires an explicit dataset license reference")
    return {
        "@context": {**CROISSANT_CONTEXT, "@base": str(release.url).rstrip("/") + "/"},
        "@type": "sc:Dataset",
        "@id": str(release.url),
        "conformsTo": CROISSANT_VERSION,
        "identifier": identity.resource_id,
        "name": identity.title,
        "version": identity.version,
        "url": str(release.url),
        "description": release.description,
        "datePublished": release.date_published.isoformat(),
        **({"citeAs": release.citation} if release.citation is not None else {}),
        "creator": [{"@type": creator.kind, "name": creator.name} for creator in release.creators],
        "license": [str(license_url) for license_url in licenses],
        "distribution": [file.to_file_object() for file in release.files],
    }


def _source_policies(
    policy: ResourcePolicyProfile,
    sources: tuple[ResourcePolicyProfile, ...],
    expected: Mapping[str, str],
) -> tuple[ResourcePolicyProfile, ...]:
    root = policy.identity.resource_id
    fingerprints = {source.identity.resource_id: source.fingerprint for source in sources}
    if len(fingerprints) != len(sources) or root in fingerprints or fingerprints != expected:
        raise ValueError("Croissant source policies differ from the reviewed fingerprint set")

    graph = {}
    for record in (policy, *sources):
        parents = set(record.derived_from)
        if record.redacted_from:
            parents.add(record.redacted_from)
        graph[record.identity.resource_id] = parents
    parents = set().union(*graph.values())
    if parents != set(fingerprints):
        raise ValueError("Croissant requires the exact available source policy closure")
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        raise ValueError("Croissant source policy closure contains a cycle") from exc
    return tuple(sorted(sources, key=lambda source: source.identity.resource_id))


def export_croissant(
    release: DatasetRelease,
    policy: ResourcePolicyProfile,
    request: ResourceUseRequest,
    *,
    expected_policy_fingerprint: str,
    source_policies: tuple[ResourcePolicyProfile, ...] = (),
    expected_source_fingerprints: Mapping[str, str] | None = None,
) -> tuple[bytes, PolicyDecision]:
    """Return canonical metadata bytes and the local derivation decision."""
    policy = policy.model_copy(deep=True)
    if policy.fingerprint != expected_policy_fingerprint:
        raise ValueError("Croissant requires the independently reviewed policy fingerprint")
    identity = policy.identity
    if identity.resource_type != "dataset" or not (identity.title and identity.version):
        raise ValueError("Croissant requires a titled and versioned dataset policy")
    if identity.content_digest != release.fingerprint:
        raise ValueError("Croissant release differs from the dataset policy inventory")
    if request.action is not ResourceAction.DERIVE or request.purpose != CROISSANT_PURPOSE:
        raise ValueError("Croissant export requires a derive request for Croissant metadata")
    if request.resource_id != identity.resource_id:
        raise ValueError("Croissant request must identify the reviewed dataset")
    reviewed_sources = tuple(source.model_copy(deep=True) for source in source_policies)
    sources = _source_policies(policy, reviewed_sources, expected_source_fingerprints or {})
    evaluator = ResourcePolicyEvaluator()
    if sources:
        decision = evaluator.evaluate_all(
            (policy, *sources), lambda resource_id: request.model_copy(update={"resource_id": resource_id}),
        )
    else:
        decision = evaluator.evaluate(policy, request)
    if decision.status is not PolicyDecisionStatus.ALLOW:
        raise ValueError(f"Croissant metadata derivation denied: {decision.status.value}")
    if release.date_published > request.now.date():
        raise ValueError("Croissant publication date cannot be after the evaluation date")

    metadata = _snapshot_metadata(release, policy)
    return canonical_json(metadata).encode("utf-8") + b"\n", decision
