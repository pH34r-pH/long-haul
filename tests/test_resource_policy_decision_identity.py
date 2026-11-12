"""Policy decisions identify their source/runtime implementation, not its authority."""
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from long_haul.resource_policy import (
    PolicyDecision,
    PolicyRule,
    ResourceIdentity,
    ResourcePolicyEvaluator,
    ResourcePolicyProfile,
    ResourceUseRequest,
)

NOW = datetime(2026, 11, 12, tzinfo=UTC)


def fixture():
    profile = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:asset", resource_type="dataset"),
        policy_id="urn:fixture:policy", policy_assigner="urn:fixture:issuer", lifecycle="active",
        permissions=[PolicyRule(rule_id="allow", actions=["ai-augment"], purposes=["research"])],
    )
    request = ResourceUseRequest(request_id="fixture", resource_id=profile.identity.resource_id,
                                 action="ai-augment", purpose="research", caller_id="fixture", now=NOW)
    return profile, request


@pytest.mark.parametrize("module", ["resource_policy.py", "resource_policy_models.py", "resource_policy_evaluator.py"])
def test_decision_identity_changes_with_policy_code(monkeypatch, module):
    profile, request = fixture()
    evaluator = ResourcePolicyEvaluator()
    original = Path.read_bytes
    baseline = evaluator.evaluate(profile, request).model_dump().get("evaluator_implementation")

    def changed(path):
        result = original(path)
        return result + b"\n# synthetic source change\n" if path.name == module else result

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert baseline
    with pytest.raises(ValueError, match="implementation.*changed"):
        evaluator.evaluate(profile, request)


def test_legacy_decision_is_not_mislabeled_as_current():
    legacy = {"request_id": "fixture", "request_fingerprint": "old", "resource_id": "urn:fixture:asset",
              "status": "UNKNOWN", "reason": "legacy", "evaluated_at": NOW.isoformat()}
    decision = PolicyDecision.model_validate(legacy)
    assert decision.model_dump().get("evaluator_implementation") is None
    assert "evaluator_implementation" not in json.loads(decision.model_dump_json())


def test_dependency_version_drift_refuses_new_decisions(monkeypatch):
    import pydantic_core

    profile, request = fixture()
    assert ResourcePolicyEvaluator().evaluate(profile, request).evaluator_implementation
    monkeypatch.setattr(pydantic_core, "__version__", "synthetic-different-version")
    with pytest.raises(ValueError, match="implementation.*changed"):
        ResourcePolicyEvaluator().evaluate(profile, request)


def test_derived_decision_binds_the_same_implementation_as_its_sources():
    profile, request = fixture()
    other = profile.model_copy(deep=True)
    other.identity.resource_id = "urn:fixture:other"
    evaluator = ResourcePolicyEvaluator()
    source = evaluator.evaluate(profile, request)
    derived = evaluator.evaluate_all([profile, other], lambda resource: request.model_copy(update={"resource_id": resource}))
    assert source.evaluator_implementation.startswith("resource-policy-evaluator-v1:sha256:")
    assert source.evaluator_implementation == evaluator.implementation_reference()
    assert PolicyDecision.model_validate_json(source.model_dump_json()).evaluator_implementation == source.evaluator_implementation
    assert derived.model_dump().get("evaluator_implementation") == source.model_dump()["evaluator_implementation"]
