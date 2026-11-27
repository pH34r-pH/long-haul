import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from long_haul.resource_policy import (
    PolicyDecisionStatus,
    ResourceAction,
    ResourcePolicyEvaluator,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from long_haul.resource_policy_interchange import (
    export_policy_jsonld,
    import_policy_jsonld,
)

EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "contracts/resource-policy/example-memory.yaml"
)


@pytest.fixture
def policy():
    authored = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    authored["identity"]["media_type"] = "text/plain"
    authored["permissions"][0].update(
        {
            "actions": ["search", "read"],
            "crew_ids": ["crew-a", "crew-b"],
            "mission_ids": ["mission-a"],
            "contract_ids": ["contract-a"],
            "repository_ids": ["repo-a"],
            "audience_ids": ["internal"],
            "valid_from": "2026-08-01T05:00:00+05:00",
            "valid_until": "2026-12-01T00:00:00Z",
            "duties": [
                {
                    "duty_id": "consent",
                    "action": "review-consent",
                    "description": "Confirm consent",
                    "requires_consent": True,
                },
                {
                    "duty_id": "review",
                    "action": "review",
                    "description": "Review scope",
                    "requires_review": True,
                },
            ],
        }
    )
    authored["quality"] = [
        {
            "metric": "coverage",
            "value": 0.5,
            "unit": "ratio",
            "evaluator_reference": "urn:fixture:quality",
        },
    ]
    authored["spdx_references"] = ["urn:fixture:spdx"]
    authored["attestation_references"] = ["urn:fixture:receipt"]
    authored["derived_from"] = ["urn:fixture:source2", "urn:fixture:source1"]
    return ResourcePolicyProfile.model_validate(authored)


def test_yaml_json_and_jsonld_share_the_exact_policy_identity(policy):
    authored = policy.model_dump(mode="json")
    yaml_policy = ResourcePolicyProfile.model_validate(
        yaml.safe_load(yaml.safe_dump(authored))
    )
    json_policy = ResourcePolicyProfile.model_validate_json(
        json.dumps(authored, sort_keys=True)
    )
    encoded = export_policy_jsonld(policy)
    assert (
        export_policy_jsonld(yaml_policy)
        == export_policy_jsonld(json_policy)
        == encoded
    )
    received = import_policy_jsonld(
        encoded.encode(), expected_fingerprint=policy.fingerprint
    )
    assert received == policy
    assert received.fingerprint == policy.fingerprint
    assert export_policy_jsonld(received) == encoded
    pretty = json.dumps(json.loads(encoded), indent=2)
    assert (
        import_policy_jsonld(pretty, expected_fingerprint=policy.fingerprint) == policy
    )


def test_import_preserves_consent_and_final_policy_decision(policy):
    policy.privacy.deletion_required = False
    received = import_policy_jsonld(
        export_policy_jsonld(policy), expected_fingerprint=policy.fingerprint
    )
    request = ResourceUseRequest(
        request_id="request-1",
        resource_id=policy.identity.resource_id,
        action=ResourceAction.AI_AUGMENT,
        purpose="mission-assistance",
        caller_id="session-1",
        crew_id="crew-1",
        mission_id="mission-1",
        now=datetime(2026, 11, 27, tzinfo=UTC),
    )
    evaluator = ResourcePolicyEvaluator()
    blocked = evaluator.evaluate(received, request)
    assert blocked == evaluator.evaluate(policy, request)
    assert blocked.status is PolicyDecisionStatus.REQUIRE_CONSENT
    consented = request.model_copy(update={"fulfilled_duties": ["current-consent"]})
    allowed = evaluator.evaluate(received, consented)
    assert allowed == evaluator.evaluate(policy, consented)
    assert allowed.status is PolicyDecisionStatus.ALLOW


@pytest.mark.parametrize(
    "field,value",
    [
        ("@context", "https://invalid.example/context.jsonld"),
        ("@id", "urn:fixture:different-record"),
        ("@type", "odrl:Offer"),
        ("odrl:permission", [{"odrl:action": "odrl:use"}]),
    ],
)
def test_rejects_different_jsonld_identity_context_or_extra_statements(
    policy, field, value
):
    document = json.loads(export_policy_jsonld(policy))
    document[field] = value
    with pytest.raises(ValueError):
        import_policy_jsonld(
            json.dumps(document), expected_fingerprint=policy.fingerprint
        )


def test_rejects_unrecognized_nested_data_and_rule_reordering(policy):
    document = json.loads(export_policy_jsonld(policy))
    permission = document["permissions"][0]
    permission["unknown"] = True
    with pytest.raises(ValueError, match="canonical record"):
        import_policy_jsonld(
            json.dumps(document), expected_fingerprint=policy.fingerprint
        )
    del permission["unknown"]
    permission["duties"].reverse()
    with pytest.raises(ValueError, match="canonical record"):
        import_policy_jsonld(
            json.dumps(document), expected_fingerprint=policy.fingerprint
        )


def test_rejects_duplicate_keys_before_interpreting_a_policy(policy):
    duplicate = export_policy_jsonld(policy).replace(
        '"deletion_required":', '"deletion_required":false,"deletion_required":'
    )
    with pytest.raises(ValueError, match="duplicate key"):
        import_policy_jsonld(duplicate, expected_fingerprint=policy.fingerprint)


def test_recomputed_fingerprint_cannot_replace_the_independent_pin(policy):
    changed = policy.model_copy(update={"prohibitions": []})
    assert changed.fingerprint != policy.fingerprint
    with pytest.raises(ValueError, match="expected fingerprint"):
        import_policy_jsonld(
            export_policy_jsonld(changed), expected_fingerprint=policy.fingerprint
        )
