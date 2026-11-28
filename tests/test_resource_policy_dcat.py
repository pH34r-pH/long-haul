import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from long_haul.resource_policy import (
    PolicyDecisionStatus,
    PolicyRule,
    ResourceIdentity,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from long_haul.resource_policy_dcat import (
    DCAT_DISTRIBUTION_PURPOSE,
    PublishedDistribution,
    export_dcat_distribution,
)
from long_haul.resource_policy_interchange import (
    export_policy_jsonld,
    import_policy_jsonld,
)

ISSUED = "2026-10-06T04:12:41Z"
NOW = "2026-11-27T14:00:00Z"
FIXTURE_BYTES = b"reviewed archive bytes"
RIGHTS = "https://archive.example/rights/v1"
LICENSE = "https://creativecommons.org/licenses/by/4.0/"
ODRL_POLICY = "https://archive.example/policy/releases/v1"
SHAPES = Path(__file__).resolve().parents[1] / "contracts/resource-policy/dcat-distribution.shacl.ttl"


@pytest.fixture
def distribution():
    return PublishedDistribution(
        dataset_id="https://archive.example/dataset/v1",
        distribution_id="https://archive.example/dataset/v1/distributions/zip",
        download_url="https://archive.example/files/v1.zip",
        title="Verified fixture archive",
        version="1.0",
        issued_at=ISSUED,
        byte_size=len(FIXTURE_BYTES),
        sha256=hashlib.sha256(FIXTURE_BYTES).hexdigest(),
        media_type="application/zip",
        publication_receipt_url="https://archive.example/receipts/v1",
        publication_receipt_sha256="a" * 64,
        license_url=LICENSE,
        rights_url=RIGHTS,
        odrl_policy_url=ODRL_POLICY,
    )


@pytest.fixture
def policy(distribution):
    return ResourcePolicyProfile(
        identity=ResourceIdentity(
            resource_id=str(distribution.distribution_id),
            resource_type="distribution",
            version=distribution.version,
            content_digest=f"sha256:{distribution.sha256}",
            media_type=distribution.media_type,
        ),
        policy_id=ODRL_POLICY,
        policy_assigner="urn:fixture:reviewer",
        lifecycle="active",
        license_references=[LICENSE],
        rights_references=[RIGHTS],
        permissions=[
            PolicyRule(
                rule_id="catalog-metadata-only",
                actions=["derive"],
                purposes=[DCAT_DISTRIBUTION_PURPOSE],
                principal_ids=["catalog-reviewer"],
                audience_ids=["public"],
            )
        ],
    )


@pytest.fixture
def request_to_derive(distribution):
    return ResourceUseRequest(
        request_id="catalog-entry",
        resource_id=str(distribution.distribution_id),
        action="derive",
        purpose=DCAT_DISTRIBUTION_PURPOSE,
        caller_id="catalog-reviewer",
        audience_id="public",
        now=NOW,
    )


@pytest.fixture
def pins(distribution, policy):
    return {
        "expected_publication_fingerprint": distribution.fingerprint,
        "expected_policy_fingerprint": policy.fingerprint,
    }


def test_emits_same_metadata_and_decision_after_policy_round_trip(
    distribution, policy, request_to_derive, pins,
):
    imported = import_policy_jsonld(export_policy_jsonld(policy), expected_fingerprint=policy.fingerprint)
    metadata, decision = export_dcat_distribution(distribution, policy, request_to_derive, **pins)
    assert (metadata, decision) == export_dcat_distribution(distribution, imported, request_to_derive, **pins)
    assert decision.status == PolicyDecisionStatus.ALLOW
    assert decision.policy_fingerprint == policy.fingerprint
    assert decision.request_fingerprint == request_to_derive.fingerprint


def test_native_terms_and_shacl_bind_the_exact_distribution(distribution, policy, request_to_derive, pins):
    rdflib = pytest.importorskip("rdflib", reason="install policy test requirements")
    validate = pytest.importorskip("pyshacl", reason="install policy test requirements").validate
    metadata, _ = export_dcat_distribution(distribution, policy, request_to_derive, **pins)
    graph = rdflib.Graph().parse(data=metadata, format="json-ld")
    dataset = rdflib.URIRef(str(distribution.dataset_id))
    asset = rdflib.URIRef(str(distribution.distribution_id))
    dcat = rdflib.Namespace("http://www.w3.org/ns/dcat#")
    dct = rdflib.Namespace("http://purl.org/dc/terms/")
    odrl = rdflib.Namespace("http://www.w3.org/ns/odrl/2/")
    spdx = rdflib.Namespace("http://spdx.org/rdf/terms#")
    assert graph.value(dataset, dcat.distribution) == asset
    assert graph.value(asset, dcat.downloadURL) == rdflib.URIRef(str(distribution.download_url))
    assert graph.value(asset, dcat.byteSize).toPython() == len(FIXTURE_BYTES)
    assert graph.value(asset, dct.license) == rdflib.URIRef(LICENSE)
    assert graph.value(asset, dct.rights) == rdflib.URIRef(RIGHTS)
    assert graph.value(asset, odrl.hasPolicy) == rdflib.URIRef(ODRL_POLICY)
    assert graph.value(asset, dct.references) == rdflib.URIRef(str(distribution.publication_receipt_url))
    checksum = graph.value(asset, spdx.checksum)
    assert graph.value(checksum, spdx.checksumValue).toPython().hex() == distribution.sha256

    shapes = rdflib.Graph().parse(SHAPES, format="turtle")
    assert validate(graph, shacl_graph=shapes)[0]
    graph.remove((checksum, spdx.checksumValue, None))
    assert not validate(graph, shacl_graph=shapes)[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("download_url", "https://archive.example/files/different.zip"),
        ("publication_receipt_sha256", "b" * 64),
        ("byte_size", 0),
        ("title", "Unreviewed metadata"),
    ],
)
def test_a_changed_publication_cannot_supply_its_own_pin(
    distribution, policy, request_to_derive, pins, field, value,
):
    changed = PublishedDistribution.model_validate({**distribution.model_dump(), field: value})
    with pytest.raises(ValueError, match="reviewed publication fingerprint"):
        export_dcat_distribution(changed, policy, request_to_derive, **pins)


@pytest.mark.parametrize("field", ["license_url", "rights_url", "odrl_policy_url"])
def test_public_rights_claim_must_match_reviewed_policy(distribution, policy, request_to_derive, pins, field):
    changed = PublishedDistribution.model_validate({
        **distribution.model_dump(), field: "https://archive.example/unreviewed",
    })
    pins["expected_publication_fingerprint"] = changed.fingerprint
    with pytest.raises(ValueError, match="differs from the reviewed policy"):
        export_dcat_distribution(changed, policy, request_to_derive, **pins)


@pytest.mark.parametrize("lifecycle", ["stale", "retracted", "expired"])
def test_current_lifecycle_blocks_projection(distribution, policy, request_to_derive, pins, lifecycle):
    policy = ResourcePolicyProfile.model_validate({**policy.model_dump(), "lifecycle": lifecycle})
    pins["expected_policy_fingerprint"] = policy.fingerprint
    with pytest.raises(ValueError, match="metadata derivation denied"):
        export_dcat_distribution(distribution, policy, request_to_derive, **pins)


@pytest.mark.parametrize(
    "changes",
    [
        {"action": "publish"},
        {"audience_id": "private"},
        {"now": "2026-10-06T04:12:40Z"},
        {"caller_id": "other-caller"},
    ],
)
def test_refuses_other_authority_or_unpublished_time(distribution, policy, request_to_derive, pins, changes):
    request_to_derive = ResourceUseRequest.model_validate({**request_to_derive.model_dump(), **changes})
    with pytest.raises(ValueError, match="DCAT"):
        export_dcat_distribution(distribution, policy, request_to_derive, **pins)


def test_refuses_unreviewed_distribution_bytes(distribution, policy, request_to_derive, pins):
    changed = distribution.model_copy(update={"sha256": "c" * 64})
    pins["expected_publication_fingerprint"] = changed.fingerprint
    with pytest.raises(ValueError, match="digest"):
        export_dcat_distribution(changed, policy, request_to_derive, **pins)


def test_refuses_a_self_asserted_policy(distribution, policy, request_to_derive, pins):
    policy.permissions[0].principal_ids = []
    with pytest.raises(ValueError, match="reviewed policy fingerprint"):
        export_dcat_distribution(distribution, policy, request_to_derive, **pins)


@pytest.mark.parametrize("url", ["https://archive.example/file?token=1", "https://user@archive.example/file"])
def test_stable_download_reference_excludes_access_secrets(distribution, url):
    with pytest.raises(ValidationError, match="stable HTTPS reference"):
        PublishedDistribution.model_validate({**distribution.model_dump(), "download_url": url})


def _source(policy, resource_id, *, derived_from=(), lifecycle="active"):
    return ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id=resource_id, resource_type="source"),
        policy_id=f"{resource_id}:policy",
        policy_assigner=policy.policy_assigner,
        lifecycle=lifecycle,
        permissions=policy.permissions,
        derived_from=list(derived_from),
    )


def test_redaction_uses_exact_transitive_policies_without_serializing_them(
    distribution, policy, request_to_derive, pins,
):
    root = _source(policy, "urn:private:root")
    parent = _source(policy, "urn:private:parent", derived_from=(root.identity.resource_id,))
    policy.redacted_from = parent.identity.resource_id
    pins["expected_policy_fingerprint"] = policy.fingerprint
    sources = (root, parent)
    source_pins = {source.identity.resource_id: source.fingerprint for source in sources}
    metadata, decision = export_dcat_distribution(
        distribution, policy, request_to_derive,
        source_policies=sources, expected_source_fingerprints=source_pins, **pins,
    )
    assert decision.source_policy_fingerprints == {
        **source_pins, policy.identity.resource_id: policy.fingerprint,
    }
    assert b"urn:private:" not in metadata
    assert b"odrl:permission" not in metadata
    with pytest.raises(ValueError, match="reviewed fingerprint set"):
        export_dcat_distribution(
            distribution, policy, request_to_derive,
            source_policies=(parent,), expected_source_fingerprints=source_pins, **pins,
        )
    with pytest.raises(ValueError, match="exact derivation"):
        export_dcat_distribution(
            distribution, policy, request_to_derive,
            source_policies=(parent,), expected_source_fingerprints={parent.identity.resource_id: parent.fingerprint},
            **pins,
        )


def test_unavailable_or_cyclic_sources_block_metadata(distribution, policy, request_to_derive, pins):
    source = _source(policy, "urn:private:source", lifecycle="unavailable")
    policy.derived_from = [source.identity.resource_id]
    pins["expected_policy_fingerprint"] = policy.fingerprint

    for state, failure in (("unavailable", "UNAVAILABLE"), ("cycle", "cycle")):
        if state == "cycle":
            source.derived_from = [source.identity.resource_id]
        with pytest.raises(ValueError, match=failure):
            export_dcat_distribution(
                distribution, policy, request_to_derive,
                source_policies=(source,), expected_source_fingerprints={source.identity.resource_id: source.fingerprint},
                **pins,
            )
