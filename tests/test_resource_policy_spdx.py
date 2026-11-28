import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from long_haul.resource_policy import (
    AuthorityClass,
    PolicyDecisionStatus,
    PolicyRule,
    ResourceAction,
    ResourceIdentity,
    ResourceLifecycle,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from long_haul.resource_policy_spdx import (
    LICENSE_INDIVIDUALS,
    SPDX_BOM_PURPOSE,
    SPDX_CONTEXT,
    SpdxBomManifest,
    SpdxPackageFact,
    export_spdx_bom,
)

CREATED = datetime(2026, 8, 2, tzinfo=UTC)
TEST_BYTES = {
    "software": b"synthetic software package",
    "model": b"synthetic model package",
    "dataset": b"synthetic dataset package",
}


def _policy(fact):
    return ResourcePolicyProfile(
        identity=ResourceIdentity(
            resource_id=fact.resource_id, resource_type=fact.kind, title=f"Synthetic {fact.kind}",
            version="1.0.0", content_digest=f"sha256:{fact.sha256}",
        ),
        authority_class=AuthorityClass.AUTHORITATIVE_CONTROL,
        lifecycle=ResourceLifecycle.ACTIVE,
        policy_id=f"{fact.resource_id}:policy", policy_assigner="reviewed-artifact-registry",
        permissions=[PolicyRule(
            rule_id="export", actions=[ResourceAction.DERIVE],
            purposes=[SPDX_BOM_PURPOSE], principal_ids=["registry-operator"],
        )],
    )


@pytest.fixture
def reviewed_bom():
    software, model, dataset = (f"urn:example:{kind}:v1" for kind in TEST_BYTES)
    facts = (
        SpdxPackageFact(
            resource_id=software, kind="software",
            sha256=hashlib.sha256(TEST_BYTES["software"]).hexdigest(),
            declared_license="MIT", concluded_license="MIT AND Apache-2.0",
            conclusion_comment="The synthetic example includes files under both licenses.",
            depends_on=(model, dataset),
        ),
        SpdxPackageFact(
            resource_id=model, kind="model",
            sha256=hashlib.sha256(TEST_BYTES["model"]).hexdigest(),
            declared_license="NOASSERTION", concluded_license="NOASSERTION",
        ),
        SpdxPackageFact(
            resource_id=dataset, kind="dataset",
            sha256=hashlib.sha256(TEST_BYTES["dataset"]).hexdigest(),
            declared_license="CC-BY-4.0", concluded_license="CC-BY-4.0",
            built_at=datetime(2026, 8, 1, tzinfo=UTC),
            download_location="https://example.org/fixtures/dataset-v1.bin", dataset_types=("structured",),
        ),
    )
    manifest = SpdxBomManifest(
        title="Synthetic repository fixture BOM", creator="External reviewer",
        creator_type="Organization", created_at=CREATED,
        root_resource_id=software, packages=facts,
    )
    request = ResourceUseRequest(
        request_id="bom-export-1", resource_id=software, action=ResourceAction.DERIVE,
        purpose=SPDX_BOM_PURPOSE, caller_id="registry-operator", now=CREATED,
    )
    return manifest, tuple(_policy(fact) for fact in facts), request


def _pins(policies):
    return {policy.identity.resource_id: policy.fingerprint for policy in policies}


def _export(manifest, policies, request):
    return export_spdx_bom(
        manifest, policies, request, expected_manifest_sha256=manifest.sha256,
        expected_policy_fingerprints=_pins(policies),
    )


def test_spdx_export_preserves_hashes_relationships_and_license_disposition(reviewed_bom):
    manifest, policies, request = reviewed_bom
    raw, decision = _export(manifest, policies, request)
    assert (raw, decision) == _export(
        SpdxBomManifest.model_validate_json(manifest.model_dump_json()), tuple(reversed(policies)), request,
    )
    document = json.loads(raw)
    assert document["@context"] == SPDX_CONTEXT
    graph = document["@graph"]
    package_types = {"software_Package", "ai_AIPackage", "dataset_DatasetPackage"}
    packages = {node["name"]: node for node in graph if node["type"] in package_types}
    for kind, content in TEST_BYTES.items():
        assert packages[f"Synthetic {kind}"]["verifiedUsing"] == [{
            "type": "Hash", "algorithm": "sha256", "hashValue": hashlib.sha256(content).hexdigest(),
        }]
    software, model, dataset = (packages[f"Synthetic {kind}"] for kind in TEST_BYTES)
    assert dataset["dataset_datasetType"] == ["structured"]
    assert dataset["builtTime"] == "2026-08-01T00:00:00Z"
    assert dataset["software_downloadLocation"] == "https://example.org/fixtures/dataset-v1.bin"
    relations = {(node["from"], node["relationshipType"]): node for node in graph if node["type"] == "Relationship"}
    assert set(relations[software["spdxId"], "dependsOn"]["to"]) == {model["spdxId"], dataset["spdxId"]}
    assert relations[model["spdxId"], "hasDeclaredLicense"]["to"] == [LICENSE_INDIVIDUALS["NOASSERTION"]]
    declared = relations[software["spdxId"], "hasDeclaredLicense"]
    concluded = relations[software["spdxId"], "hasConcludedLicense"]
    assert declared["to"] != concluded["to"]
    assert concluded["comment"] == manifest.packages[0].conclusion_comment
    assert decision.status is PolicyDecisionStatus.ALLOW
    assert decision.source_policy_fingerprints == _pins(policies)
    assert b"registry-operator" not in raw and b"reviewed-artifact-registry" not in raw


def test_manifest_and_artifact_fingerprints_are_independent(reviewed_bom):
    manifest, policies, request = reviewed_bom
    changed = manifest.model_copy(update={"title": "Replaced candidate title"})
    with pytest.raises(ValueError, match="independently reviewed manifest"):
        export_spdx_bom(changed, policies, request, expected_manifest_sha256=manifest.sha256,
                        expected_policy_fingerprints=_pins(policies))
    policies[0].identity.content_digest = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="kind or SHA-256"):
        _export(manifest, policies, request)


def test_policy_change_does_not_reuse_old_reviewed_pin(reviewed_bom):
    manifest, policies, request = reviewed_bom
    pins = _pins(policies)
    policies[0].identity.title = "Changed after review"
    with pytest.raises(ValueError, match="independently reviewed policy fingerprint set"):
        export_spdx_bom(manifest, policies, request, expected_manifest_sha256=manifest.sha256,
                        expected_policy_fingerprints=pins)


@pytest.mark.parametrize("changes", [
    {"resource_id": "urn:example:other:root"}, {"action": ResourceAction.PUBLISH},
    {"purpose": "private-release"}, {"caller_id": "different-principal"},
])
def test_spdx_export_rejects_wrong_identity_or_use(reviewed_bom, changes):
    manifest, policies, request = reviewed_bom
    with pytest.raises(ValueError):
        _export(manifest, policies, request.model_copy(update=changes))


def test_all_transitive_sources_must_be_reviewed_and_allow_the_use(reviewed_bom):
    manifest, policies, request = reviewed_bom
    source = policies[1].model_copy(deep=True)
    source.identity.resource_id = "urn:example:upstream-evidence:v1"
    policies[1].derived_from = [source.identity.resource_id]
    with pytest.raises(ValueError, match="exact complete source"):
        _export(manifest, policies, request)
    raw, decision = _export(manifest, (*policies, source), request)
    assert decision.source_policy_fingerprints == _pins((*policies, source))
    assert source.identity.resource_id.encode() not in raw
    source.lifecycle = ResourceLifecycle.RETRACTED
    with pytest.raises(ValueError, match="RETRACTED"):
        _export(manifest, (*policies, source), request)
    source.redacted_from = policies[1].identity.resource_id
    with pytest.raises(ValueError, match="cycle"):
        _export(manifest, (*policies, source), request)


@pytest.mark.parametrize("changes", [
    {"kind": "dataset"}, {"sha256": "non-cryptographic"}, {"declared_license": "unreviewed-guess"},
    {"concluded_license": "MIT WITH Apache-2.0"}, {"download_location": "https://example.org/a?token=x"},
])
def test_artifact_descriptions_require_supported_typed_facts(reviewed_bom, changes):
    manifest, _, _ = reviewed_bom
    with pytest.raises(ValidationError):
        SpdxPackageFact.model_validate({**manifest.packages[0].model_dump(), **changes})


def test_missing_or_unreferenced_dependencies_fail_offline(reviewed_bom):
    manifest, _, _ = reviewed_bom
    software, model, dataset = manifest.packages
    with pytest.raises(ValidationError, match="dependency is missing"):
        SpdxBomManifest.model_validate({**manifest.model_dump(), "packages": (software, model)})
    detached = software.model_copy(update={"depends_on": (dataset.resource_id,)})
    with pytest.raises(ValidationError, match="outside the root dependencies"):
        SpdxBomManifest.model_validate({**manifest.model_dump(), "packages": (detached, model, dataset)})


def test_offline_official_spdx_301_validation(reviewed_bom):
    directory = os.environ.get("LONG_HAUL_SPDX_301_ASSETS")
    if directory is None:
        pytest.skip("optional pinned SPDX validation assets are not configured")
    import jsonschema
    from pyshacl import validate
    from rdflib import Graph

    official = {
        "spdx-json-schema.json": "582c64e809d5b3ef9bd0c4de13a32391b47b0284a3e8d199569fb96f649234b1",
        "spdx-context.jsonld": "c72b0928f094c83e5c127784edb1ebca2af74a104fcacc007c332b23cbc788bd",
        "spdx-model.ttl": "30ebb4af2d70a9809044ef46f44cc3dc5125226d70f818a50ed2e1d5f404c593",
    }
    assets = {name: (Path(directory) / name).read_bytes() for name in official}
    assert {key: hashlib.sha256(value).hexdigest() for key, value in assets.items()} == official
    raw, _ = _export(*reviewed_bom)
    doc = json.loads(raw)
    jsonschema.Draft202012Validator(
        json.loads(assets["spdx-json-schema.json"]), format_checker=jsonschema.FormatChecker(),
    ).validate(doc)
    context = json.loads(assets["spdx-context.jsonld"])["@context"]
    ontology = Graph().parse(data=assets["spdx-model.ttl"].decode(), format="turtle")
    # The official namespace is fixed in the serialized document. Expand with
    # the independently pinned bytes so the validator never fetches the URL.
    graph = Graph().parse(data=json.dumps({**doc, "@context": context}), format="json-ld")
    conforms, _, report = validate(graph, shacl_graph=ontology, ont_graph=ontology, inference="none", advanced=True)
    assert conforms, report
