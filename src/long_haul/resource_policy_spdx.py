"""Project an admitted package inventory to an SPDX 3.0.1 bill of materials."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from graphlib import CycleError, TopologicalSorter
from typing import Annotated, Any, Literal

from license_expression import ExpressionError, get_spdx_licensing
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

SPDX_CONTEXT = "https://spdx.org/rdf/3.0.1/spdx-context.jsonld"
SPDX_BOM_PURPOSE = "spdx-bom"
_SPDX_LICENSING = get_spdx_licensing()
PACKAGE_TYPES = {"software": "software_Package", "model": "ai_AIPackage", "dataset": "dataset_DatasetPackage"}
ADDITIONAL_PROFILES = {"model": "ai", "dataset": "dataset"}
LICENSE_INDIVIDUALS = {
    "NONE": "https://spdx.org/rdf/3.0.1/terms/ExpandedLicensing/NoneLicense",
    "NOASSERTION": "https://spdx.org/rdf/3.0.1/terms/ExpandedLicensing/NoAssertionLicense",
}
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
DatasetType = Literal[
    "audio", "categorical", "graph", "image", "noAssertion", "numeric", "other",
    "sensor", "structured", "syntactic", "text", "timeseries", "timestamp", "video",
]


def _license_expression(value: str) -> str:
    if value in LICENSE_INDIVIDUALS:
        return value
    try:
        parsed = _SPDX_LICENSING.parse(value, validate=True, strict=True)
    except ExpressionError as exc:
        raise ValueError("expected a public SPDX license expression") from exc
    return str(parsed)


def _download_url(value: AnyHttpUrl) -> AnyHttpUrl:
    if value.scheme != "https" or any(
        part is not None for part in (value.username, value.password, value.query, value.fragment)
    ):
        raise ValueError("SPDX download location requires HTTPS without credentials, query, or fragment")
    return value


LicenseFact = Annotated[Text, AfterValidator(_license_expression)]
DownloadUrl = Annotated[AnyHttpUrl, AfterValidator(_download_url)]


class SpdxPackageFact(BaseModel):
    """Caller-reviewed facts for one immutable package."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_id: Text
    kind: Literal["software", "model", "dataset"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    declared_license: LicenseFact
    concluded_license: LicenseFact
    conclusion_comment: Text | None = None
    depends_on: tuple[Text, ...] = ()
    built_at: AwareDatetime | None = None
    download_location: DownloadUrl | None = None
    dataset_types: tuple[DatasetType, ...] = ()

    @model_validator(mode="after")
    def validate_package(self) -> SpdxPackageFact:
        if self.kind == "dataset" and not (self.built_at and self.download_location and self.dataset_types):
            raise ValueError("SPDX dataset requires a build time, download location, and dataset types")
        if self.kind != "dataset" and self.dataset_types:
            raise ValueError("only a dataset can declare SPDX dataset types")
        if len(set(self.depends_on)) != len(self.depends_on) or self.resource_id in self.depends_on:
            raise ValueError("SPDX dependencies must be distinct other packages")
        if len(set(self.dataset_types)) != len(self.dataset_types):
            raise ValueError("SPDX dataset types must be distinct")
        if "noAssertion" in self.dataset_types and len(self.dataset_types) > 1:
            raise ValueError("SPDX noAssertion cannot accompany a known dataset type")
        return self


class SpdxBomManifest(BaseModel):
    """Independently pinned source facts for an SPDX export."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: Text
    creator: Text
    creator_type: Literal["Organization", "Person"]
    created_at: AwareDatetime
    root_resource_id: Text
    packages: tuple[SpdxPackageFact, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_inventory(self) -> SpdxBomManifest:
        by_id = {package.resource_id: package for package in self.packages}
        if len(by_id) != len(self.packages) or self.root_resource_id not in by_id:
            raise ValueError("SPDX inventory requires unique packages and a known root")
        visited = set()
        pending = [self.root_resource_id]
        while pending:
            resource_id = pending.pop()
            if resource_id in visited:
                continue
            if resource_id not in by_id:
                raise ValueError("SPDX dependency is missing from the package inventory")
            visited.add(resource_id)
            pending.extend(by_id[resource_id].depends_on)
        if visited != by_id.keys():
            raise ValueError("SPDX package inventory includes packages outside the root dependencies")
        return self

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_json_bytes(self.model_dump(mode="json"))).hexdigest()


def _json_bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _policy_inventory(
    manifest: SpdxBomManifest, policies: tuple[ResourcePolicyProfile, ...], expected: Mapping[str, str],
) -> dict[str, ResourcePolicyProfile]:
    records = {policy.identity.resource_id: policy.model_copy(deep=True) for policy in policies}
    fingerprints = {resource_id: policy.fingerprint for resource_id, policy in records.items()}
    if len(records) != len(policies) or fingerprints != expected:
        raise ValueError("SPDX requires the independently reviewed policy fingerprint set")

    package_ids = {package.resource_id for package in manifest.packages}
    closure = set(package_ids)
    graph = {}
    for resource_id, policy in records.items():
        parents = set(policy.derived_from)
        if policy.redacted_from:
            parents.add(policy.redacted_from)
        graph[resource_id] = parents
        closure.update(parents)
    if closure != records.keys():
        raise ValueError("SPDX requires the exact complete source policy closure")
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        raise ValueError("SPDX source policy closure contains a cycle") from exc
    for package in manifest.packages:
        identity = records[package.resource_id].identity
        if identity.resource_type != package.kind or identity.content_digest != f"sha256:{package.sha256}":
            raise ValueError("SPDX package kind or SHA-256 differs from its reviewed policy identity")
        if not (identity.title and identity.version):
            raise ValueError("SPDX package requires a reviewed name and version")
    return records


def _element(kind: str, spdx_id: str, **properties: Any) -> dict[str, Any]:
    return {"type": kind, "spdxId": spdx_id, "creationInfo": "_:creationinfo", **properties}


def _package_node(fact: SpdxPackageFact, spdx_id: str, policy: ResourcePolicyProfile) -> dict[str, Any]:
    node = _element(
        PACKAGE_TYPES[fact.kind], spdx_id, name=policy.identity.title,
        software_packageVersion=policy.identity.version,
        verifiedUsing=[{"type": "Hash", "algorithm": "sha256", "hashValue": fact.sha256}],
    )
    if fact.built_at:
        node["builtTime"] = _timestamp(fact.built_at)
    if fact.download_location:
        node["software_downloadLocation"] = str(fact.download_location)
    if fact.dataset_types:
        node["dataset_datasetType"] = sorted(fact.dataset_types)
    return node


def _license_nodes(manifest: SpdxBomManifest, namespace: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    expressions = {expression for fact in manifest.packages for expression in (
        fact.declared_license, fact.concluded_license,
    )}
    identifiers = dict(LICENSE_INDIVIDUALS)
    nodes = []
    for index, expression in enumerate(sorted(expressions - LICENSE_INDIVIDUALS.keys()), start=1):
        identifier = f"{namespace}:license:{index}"
        identifiers[expression] = identifier
        nodes.append(_element(
            "simplelicensing_LicenseExpression", identifier, simplelicensing_licenseExpression=expression,
        ))
    return nodes, identifiers


def _relationship(source: str, kind: str, targets: list[str], comment: str | None = None) -> dict[str, Any]:
    node = _element("Relationship", f"{source}:{kind}", relationshipType=kind, **{"from": source, "to": targets})
    if comment:
        node["comment"] = comment
    return node


def _graph(manifest: SpdxBomManifest, policies: dict[str, ResourcePolicyProfile]) -> list[dict[str, Any]]:
    packages = sorted(manifest.packages, key=lambda fact: fact.resource_id)
    identities = [policies[fact.resource_id].identity for fact in packages]
    version = hashlib.sha256(_json_bytes({
        "manifest": manifest.sha256,
        "names_and_versions": [(identity.title, identity.version) for identity in identities],
    })).hexdigest()
    namespace = f"urn:long-haul:spdx:{version}"
    creator = f"{namespace}:creator"
    bom = f"{namespace}:bom"
    identifiers = {fact.resource_id: f"{namespace}:package:{index}" for index, fact in enumerate(packages, start=1)}
    profiles = ["core", "software", "simpleLicensing"]
    profiles.extend(sorted({ADDITIONAL_PROFILES[fact.kind] for fact in packages if fact.kind in ADDITIONAL_PROFILES}))
    if any({fact.declared_license, fact.concluded_license} & LICENSE_INDIVIDUALS.keys() for fact in packages):
        profiles.append("expandedLicensing")
    root = identifiers[manifest.root_resource_id]
    inventory, licenses = _license_nodes(manifest, namespace)
    for fact in packages:
        identifier = identifiers[fact.resource_id]
        inventory.append(_package_node(fact, identifier, policies[fact.resource_id]))
        inventory.append(_relationship(identifier, "hasDeclaredLicense", [licenses[fact.declared_license]]))
        inventory.append(_relationship(
            identifier, "hasConcludedLicense", [licenses[fact.concluded_license]], fact.conclusion_comment,
        ))
        if fact.depends_on:
            inventory.append(_relationship(identifier, "dependsOn", sorted(identifiers[target] for target in fact.depends_on)))
    return [
        {"type": "CreationInfo", "@id": "_:creationinfo", "specVersion": "3.0.1",
         "createdBy": [creator], "created": _timestamp(manifest.created_at)},
        _element(manifest.creator_type, creator, name=manifest.creator),
        _element("SpdxDocument", f"{namespace}:document", name=manifest.title,
                 profileConformance=profiles, rootElement=[bom]),
        _element("software_Sbom", bom, name=manifest.title, profileConformance=profiles, rootElement=[root],
                 element=[node["spdxId"] for node in inventory]),
        *inventory,
    ]


def export_spdx_bom(
    manifest: SpdxBomManifest,
    policies: tuple[ResourcePolicyProfile, ...],
    request: ResourceUseRequest,
    *,
    expected_manifest_sha256: str,
    expected_policy_fingerprints: Mapping[str, str],
) -> tuple[bytes, PolicyDecision]:
    """Return SPDX metadata and the decision for the admitted derivation."""
    if manifest.sha256 != expected_manifest_sha256:
        raise ValueError("SPDX inventory differs from the independently reviewed manifest")
    if request.resource_id != manifest.root_resource_id:
        raise ValueError("SPDX request must identify the reviewed BOM root")
    if request.action is not ResourceAction.DERIVE or request.purpose != SPDX_BOM_PURPOSE:
        raise ValueError("SPDX export requires a derive request for a BOM")
    records = _policy_inventory(manifest, policies, expected_policy_fingerprints)
    decision = ResourcePolicyEvaluator().evaluate_all(
        (records[key] for key in sorted(records)),
        lambda resource_id: request.model_copy(update={"resource_id": resource_id}),
    )
    if decision.status is not PolicyDecisionStatus.ALLOW:
        raise ValueError(f"SPDX metadata derivation denied: {decision.status.value}")
    return _json_bytes({"@context": SPDX_CONTEXT, "@graph": _graph(manifest, records)}), decision
