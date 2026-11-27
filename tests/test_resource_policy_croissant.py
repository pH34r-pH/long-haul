import hashlib
import json
from datetime import UTC, date, datetime

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
from long_haul.resource_policy_croissant import (
    CROISSANT_PURPOSE,
    CROISSANT_VERSION,
    DatasetCreator,
    DatasetFile,
    DatasetRelease,
    export_croissant,
)


@pytest.fixture
def reviewed_dataset():
    release = DatasetRelease(
        url="https://example.org/corpora/repository-observations/1.0.0",
        description="Synthetic released dataset fixture.",
        date_published=date(2026, 8, 1),
        citation="Example dataset team. Repository observations fixture, version 1.0.0 (2026).",
        creators=(DatasetCreator(kind="Organization", name="Example dataset team"),),
        files=(
            DatasetFile(
                name="observations.csv",
                content_url="https://example.org/corpora/repository-observations/1.0.0/observations.csv",
                media_type="text/csv",
                sha256=hashlib.sha256(b"attempt,events\na1,3\n").hexdigest(),
            ),
        ),
    )
    policy = ResourcePolicyProfile(
        identity=ResourceIdentity(
            resource_id="urn:example:repository-observations:1.0.0", resource_type="dataset",
            title="Repository observations fixture", version="1.0.0", content_digest=release.fingerprint,
        ),
        policy_id="urn:example:policy", policy_assigner="reviewed-catalog",
        authority_class=AuthorityClass.AUTHORITATIVE_CONTROL, lifecycle=ResourceLifecycle.ACTIVE,
        license_references=["https://creativecommons.org/licenses/by/4.0/"],
        permissions=[
            PolicyRule(rule_id="derive", actions=[ResourceAction.DERIVE],
                       purposes=[CROISSANT_PURPOSE], principal_ids=["catalog-operator"]),
        ],
        prohibitions=[PolicyRule(rule_id="publish", actions=[ResourceAction.PUBLISH])],
    )
    request = ResourceUseRequest(
        request_id="export-1", caller_id="catalog-operator",
        resource_id=policy.identity.resource_id, action=ResourceAction.DERIVE, purpose=CROISSANT_PURPOSE,
        now=datetime(2026, 8, 2, tzinfo=UTC),
    )
    return release, policy, request


def test_export_croissant_binds_exact_policy_and_metadata(reviewed_dataset):
    release, policy, request = reviewed_dataset
    raw, decision = export_croissant(release, policy, request, expected_policy_fingerprint=policy.fingerprint)
    assert raw == export_croissant(
        DatasetRelease.model_validate_json(release.model_dump_json()), policy, request,
        expected_policy_fingerprint=policy.fingerprint,
    )[0]
    document = json.loads(raw)
    assert document["conformsTo"] == CROISSANT_VERSION
    assert document["identifier"] == policy.identity.resource_id
    assert document["datePublished"] == "2026-08-01"
    assert document["citeAs"] == release.citation
    assert document["license"] == policy.license_references
    assert document["distribution"] == [{
        "@id": "files/observations.csv", "@type": "cr:FileObject", "name": "observations.csv",
        "contentUrl": str(release.files[0].content_url), "encodingFormat": "text/csv",
        "sha256": release.files[0].sha256,
    }]
    assert decision.status is PolicyDecisionStatus.ALLOW
    assert decision.policy_fingerprint == policy.fingerprint
    assert decision.matched_permissions == ["derive"]
    assert decision.evaluator_implementation
    assert b"catalog-operator" not in raw and b"urn:example:policy" not in raw


def test_changed_file_or_policy_cannot_reuse_reviewed_identity(reviewed_dataset):
    release, policy, request = reviewed_dataset
    pin = policy.fingerprint
    other_file = release.files[0].model_copy(update={"sha256": "f" * 64})
    changed = release.model_copy(update={"files": (other_file,)})
    with pytest.raises(ValueError, match="differs from the dataset policy inventory"):
        export_croissant(changed, policy, request, expected_policy_fingerprint=pin)
    policy.license_references = ["https://example.org/other-license"]
    with pytest.raises(ValueError, match="independently reviewed policy fingerprint"):
        export_croissant(release, policy, request, expected_policy_fingerprint=pin)


@pytest.mark.parametrize("state", [ResourceLifecycle.RETRACTED, ResourceLifecycle.EXPIRED, ResourceLifecycle.STALE])
def test_stale_or_retracted_resource_cannot_export(reviewed_dataset, state):
    release, policy, request = reviewed_dataset
    policy.lifecycle = state
    with pytest.raises(ValueError, match="metadata derivation denied"):
        export_croissant(release, policy, request, expected_policy_fingerprint=policy.fingerprint)


@pytest.mark.parametrize("updates", [
    {"purpose": "another-derivation"}, {"action": ResourceAction.PUBLISH}, {"caller_id": "unknown-caller"},
    {"resource_id": "urn:example:unrelated"}, {"now": datetime(2026, 7, 30, tzinfo=UTC)},
])
def test_export_requires_the_reviewed_use_scope(reviewed_dataset, updates):
    release, policy, request = reviewed_dataset
    with pytest.raises(ValueError):
        export_croissant(
            release, policy, request.model_copy(update=updates), expected_policy_fingerprint=policy.fingerprint,
        )


@pytest.mark.parametrize("updates", [
    {"content_url": "https://example.org/a?token=value"}, {"content_url": "https://user:secret@example.org/a"},
    {"content_url": "file:///tmp/data.csv"}, {"name": "../data.csv"}, {"sha256": "missing"},
])
def test_file_descriptors_reject_unsafe_or_unpinned_values(reviewed_dataset, updates):
    release, _, _ = reviewed_dataset
    with pytest.raises(ValidationError):
        DatasetFile.model_validate({**release.files[0].model_dump(), **updates})


def test_release_rejects_duplicate_file_ids_and_unreviewed_policy_fields(reviewed_dataset):
    release, _, _ = reviewed_dataset
    with pytest.raises(ValidationError, match="unique"):
        DatasetRelease.model_validate({**release.model_dump(), "files": (release.files[0], release.files[0])})
    with pytest.raises(ValidationError, match="Extra inputs"):
        DatasetRelease.model_validate({**release.model_dump(), "permissions": ["publish"]})


def _source_policy(policy, identity, *, derived_from=()):
    return ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id=identity, resource_type="evidence"),
        authority_class=policy.authority_class, lifecycle=ResourceLifecycle.ACTIVE,
        policy_id=identity + ":policy", policy_assigner=policy.policy_assigner,
        permissions=policy.permissions, derived_from=derived_from,
    )


def test_derived_release_requires_each_reviewed_source_policy(reviewed_dataset):
    release, policy, request = reviewed_dataset
    leaf = _source_policy(policy, "urn:example:leaf")
    parent = _source_policy(policy, "urn:example:parent", derived_from=[leaf.identity.resource_id])
    policy.derived_from = [parent.identity.resource_id]
    pins = {source.identity.resource_id: source.fingerprint for source in (parent, leaf)}
    kwargs = {"expected_policy_fingerprint": policy.fingerprint, "expected_source_fingerprints": pins}
    raw, decision = export_croissant(release, policy, request, source_policies=(leaf, parent), **kwargs)
    assert decision.source_policy_fingerprints == {**pins, policy.identity.resource_id: policy.fingerprint}
    assert (raw, decision) == export_croissant(release, policy, request, source_policies=(parent, leaf), **kwargs)
    with pytest.raises(ValueError, match="reviewed fingerprint set"):
        export_croissant(release, policy, request, source_policies=(parent,), **kwargs)
    with pytest.raises(ValueError, match="exact available source"):
        export_croissant(
            release, policy, request, source_policies=(parent,), expected_policy_fingerprint=policy.fingerprint,
            expected_source_fingerprints={parent.identity.resource_id: parent.fingerprint},
        )
    leaf.prohibitions = [PolicyRule(rule_id="explicit-denial", actions=[ResourceAction.DERIVE])]
    pins[leaf.identity.resource_id] = leaf.fingerprint
    with pytest.raises(ValueError, match="CONFLICT"):
        export_croissant(release, policy, request, source_policies=(parent, leaf), **kwargs)


def test_source_policy_closure_rejects_cycles_and_unrelated_sources(reviewed_dataset):
    release, policy, request = reviewed_dataset
    parent = _source_policy(policy, "urn:example:parent")
    # Redaction lineage is also an input policy.
    policy.redacted_from = parent.identity.resource_id
    parent.derived_from = [parent.identity.resource_id]
    with pytest.raises(ValueError, match="contains a cycle"):
        export_croissant(
            release, policy, request, expected_policy_fingerprint=policy.fingerprint,
            source_policies=(parent,), expected_source_fingerprints={parent.identity.resource_id: parent.fingerprint},
        )
    policy.redacted_from = None
    parent.derived_from = []
    with pytest.raises(ValueError, match="exact available source"):
        export_croissant(
            release, policy, request, expected_policy_fingerprint=policy.fingerprint,
            source_policies=(parent,), expected_source_fingerprints={parent.identity.resource_id: parent.fingerprint},
        )


@pytest.mark.parametrize("licenses", [[], ["not-a-license-url"]])
def test_unknown_license_is_not_inferred_from_dataset_access(reviewed_dataset, licenses):
    release, policy, request = reviewed_dataset
    policy.license_references = licenses
    with pytest.raises(ValueError):
        export_croissant(release, policy, request, expected_policy_fingerprint=policy.fingerprint)
