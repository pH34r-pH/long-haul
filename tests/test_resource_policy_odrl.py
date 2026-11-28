import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from long_haul.resource_policy import (
    PolicyDuty,
    PolicyRule,
    ResourceAction,
    ResourceIdentity,
    ResourceLifecycle,
    ResourcePolicyProfile,
)
from long_haul.resource_policy_odrl import (
    NATIVE_ODRL_PROFILE,
    export_native_odrl,
    verify_native_odrl,
)

NOW = datetime(2026, 8, 2, 12, tzinfo=UTC)


@pytest.fixture
def reviewed_policy():
    consent = PolicyDuty(duty_id="written-consent", action="obtain-consent",
                         description="Retain the reviewed consent evidence.", requires_consent=True)
    remedy_note = PolicyDuty(duty_id="incident-review", action="review-incident",
                             description="Recorded follow-up duty.", requires_review=True)
    return ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="dataset/hr-v1", resource_type="dataset",
                                  content_digest="sha256:" + "1" * 64),
        lifecycle=ResourceLifecycle.ACTIVE,
        policy_id="review/mixed-scope-v1",
        policy_assigner="library/team",
        permissions=[
            PolicyRule(
                rule_id="research-use", actions=[ResourceAction.READ, ResourceAction.SEARCH],
                purposes=["evaluation", "analysis"], principal_ids=["participant:1", "participant:2"],
                crew_ids=["crew-a", "crew-b"], mission_ids=["mission"], contract_ids=["contract"],
                repository_ids=["repo"], audience_ids=["team"], valid_from=NOW, valid_until=NOW,
                duties=[consent], source_policy_reference="approved:original/v1",
            ),
            PolicyRule(rule_id="cleanup", actions=[ResourceAction.DELETE]),
        ],
        prohibitions=[
            PolicyRule(rule_id="no-private-use", actions=[ResourceAction.READ], crew_ids=["crew-private"],
                       duties=[remedy_note]),
            PolicyRule(rule_id="no-training", actions=[ResourceAction.AI_TRAINING]),
        ],
    )


def _export(profile):
    return export_native_odrl(profile, expected_policy_fingerprint=profile.fingerprint)


def _document(profile):
    return json.loads(_export(profile))


def _operands(atom):
    return {c["odrl:leftOperand"]["@id"]: c["odrl:rightOperand"]
            for c in atom.get("odrl:constraint", []) if c["@type"] == "odrl:Constraint"}


def test_project_atomic_scopes_and_reviewed_duties(reviewed_policy):
    document = _document(reviewed_policy)
    policy, *nodes = document["@graph"]
    assert policy["@type"] == "odrl:Set"
    assert policy["odrl:profile"]["@id"] == NATIVE_ODRL_PROFILE
    assert policy["odrl:conflict"]["@id"] == "odrl:invalid"
    assert policy["lh:sourceFingerprint"] == reviewed_policy.fingerprint
    research = [row for row in policy["odrl:permission"] if row["lh:ruleId"] == "research-use"]
    assert len(research) == 4
    assert len({(row["odrl:action"]["@id"], row["odrl:assignee"]["@id"]) for row in research}) == 4
    one = research[0]
    members = {c["odrl:leftOperand"]["@id"]: {
        item["odrl:rightOperand"] for item in group["odrl:or"]["@list"]
    } for group in one["odrl:constraint"] if group["@type"] == "odrl:LogicalConstraint"
      for c in group["odrl:or"]["@list"][:1]}
    assert members == {"lh:purpose": {"analysis", "evaluation"}, "lh:crew": {"crew-a", "crew-b"}}
    assert {key: _operands(one)[key] for key in ("lh:mission", "lh:contract", "lh:repository", "lh:audience")} == {
        "lh:mission": "mission", "lh:contract": "contract", "lh:repository": "repo", "lh:audience": "team",
    }
    assert {c["odrl:operator"]["@id"] for c in one["odrl:constraint"]
            if c.get("odrl:leftOperand") == {"@id": "odrl:dateTime"}} == {"odrl:gteq", "odrl:lteq"}
    assert _operands(one)["odrl:dateTime"] == {"@type": "xsd:dateTime", "@value": "2026-08-02T12:00:00Z"}
    assert one["lh:sourcePolicyReference"] == "approved:original/v1"
    assert one["odrl:duty"][0]["lh:requiresConsent"]
    assert {node["lh:actionName"] for node in nodes if ":action:" in node["@id"]} == {
        action.value for action in ResourceAction
    }


def test_missing_purpose_is_distinct_from_confirmed_absence(reviewed_policy):
    policy = _document(reviewed_policy)["@graph"][0]
    cleanup = next(rule for rule in policy["odrl:permission"] if rule["lh:ruleId"] == "cleanup")
    prohibited = next(rule for rule in policy["odrl:prohibition"] if rule["lh:ruleId"] == "no-private-use")
    assert _operands(cleanup) == {
        "lh:admittedPolicy": reviewed_policy.fingerprint, "lh:purposeState": "known-absent",
    }
    assert "odrl:assignee" not in cleanup
    assert _operands(prohibited) == {"lh:crew": "crew-private"}
    assert prohibited["lh:declaredProhibitionDuty"][0]["lh:requiresReview"]
    assert "odrl:remedy" not in prohibited


def test_project_lifecycle_retention_and_lineage_with_no_new_permission(reviewed_policy):
    reviewed_policy.lifecycle = ResourceLifecycle.RETRACTED
    reviewed_policy.invalidated_at = NOW
    reviewed_policy.privacy.retention_until = NOW
    reviewed_policy.privacy.deletion_required = True
    reviewed_policy.privacy.purposes = ["evaluation"]
    reviewed_policy.derived_from = ["original/input"]
    reviewed_policy.redacted_from = "sensitive/input"
    policy, asset, *_ = _document(reviewed_policy)["@graph"]
    assert asset["lh:lifecycle"] == "retracted"
    assert asset["lh:invalidatedAt"] == asset["lh:retentionUntil"]
    assert asset["lh:deletionRequired"]
    assert asset["lh:privacyPurpose"] == ["evaluation"]
    assert asset["lh:derivedFrom"] == [{"@id": "urn:long-haul:odrl:asset:original%2Finput"}]
    assert asset["lh:redactedFrom"] == {"@id": "urn:long-haul:odrl:asset:sensitive%2Finput"}
    assert all(_operands(rule)["lh:admittedPolicy"] == reviewed_policy.fingerprint
               for rule in policy["odrl:permission"])


def test_exact_projection_rejects_modified_graph_and_policy(reviewed_policy):
    source = _export(reviewed_policy)
    pin = reviewed_policy.fingerprint
    verify_native_odrl(source, reviewed_policy, expected_policy_fingerprint=pin)
    assert source == _export(ResourcePolicyProfile.model_validate_json(reviewed_policy.model_dump_json()))
    for tampered in (
        source.replace(b"crew-private", b"crew-allowed"), source + b" ",
        b'{"@context": "https://example.invalid/remote-context"}',
    ):
        with pytest.raises(ValueError, match="differs from the reviewed policy projection"):
            verify_native_odrl(tampered, reviewed_policy, expected_policy_fingerprint=pin)
    reviewed_policy.prohibitions.pop()
    with pytest.raises(ValueError, match="requires the independently reviewed policy fingerprint"):
        verify_native_odrl(source, reviewed_policy, expected_policy_fingerprint=pin)


def test_empty_policy_cannot_serialize_as_an_odrl_set(reviewed_policy):
    reviewed_policy.permissions.clear()
    reviewed_policy.prohibitions.clear()
    with pytest.raises(ValueError, match="at least one rule"):
        _export(reviewed_policy)


def test_offline_native_odrl_rdf_and_shapes(reviewed_policy):
    rdflib = pytest.importorskip("rdflib")
    validate = pytest.importorskip("pyshacl").validate
    ns = rdflib.Namespace(NATIVE_ODRL_PROFILE + "/")
    odrl = rdflib.Namespace("http://www.w3.org/ns/odrl/2/")
    shapes = Path(__file__).resolve().parents[1] / "contracts/resource-policy/native-odrl.shacl.ttl"
    graph = rdflib.Graph().parse(data=_export(reviewed_policy).decode(), format="json-ld")
    shape_graph = rdflib.Graph().parse(shapes, format="turtle")
    conforms, _, report = validate(graph, shacl_graph=shape_graph)
    assert conforms, report
    permission = next(graph.subjects(rdflib.RDF.type, odrl.Permission))
    admission, = [c for c in graph.objects(permission, odrl.constraint) if
                  (c, odrl.leftOperand, ns.admittedPolicy) in graph]
    graph.set((admission, odrl.rightOperand, rdflib.Literal("unauthenticated-policy")))
    conforms, _, report = validate(graph, shacl_graph=shape_graph)
    assert not conforms and "exact source fingerprint" in report
    graph.remove((permission, odrl.constraint, admission))
    conforms, _, _ = validate(graph, shacl_graph=shape_graph)
    assert not conforms
