import json
from pathlib import Path

import yaml

from long_haul.resource_policy import (
    RESOURCE_POLICY_PROFILE_ID,
    ResourcePolicyProfile,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "contracts" / "resource-policy"


def test_profile_declares_w3c_application_profile_dependencies():
    profile = json.loads((PROFILE_DIR / "profile.jsonld").read_text(encoding="utf-8"))
    assert profile["id"] == RESOURCE_POLICY_PROFILE_ID
    assert profile["type"] == "prof:Profile"
    dependencies = set(profile["prof:isProfileOf"])
    assert "https://www.w3.org/TR/dx-prof/" in dependencies
    assert "https://www.w3.org/TR/odrl-model/" in dependencies
    assert "https://www.w3.org/TR/prov-o/" in dependencies


def test_example_memory_validates_against_runtime_projection():
    payload = yaml.safe_load(
        (PROFILE_DIR / "example-memory.yaml").read_text(encoding="utf-8")
    )
    policy = ResourcePolicyProfile.model_validate(payload)
    assert policy.identity.resource_id == "urn:nacre:memory:example-1"
    assert policy.fingerprint.startswith("resource-policy-v1:")


def test_jsonld_context_and_shacl_are_present():
    context = json.loads((PROFILE_DIR / "context.jsonld").read_text(encoding="utf-8"))
    assert context["@context"]["odrl"] == "http://www.w3.org/ns/odrl/2/"
    shapes = (PROFILE_DIR / "profile.shacl.ttl").read_text(encoding="utf-8")
    assert "lh:GovernedResourceShape" in shapes
    assert RESOURCE_POLICY_PROFILE_ID in shapes
