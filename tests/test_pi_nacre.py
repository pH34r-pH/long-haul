from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from long_haul.adapters.pi_nacre import (
    PiNacreLaunchOptions,
    PiNacreScope,
    build_pi_nacre_context,
    nacre_policy_receipt,
    pi_nacre_environment,
    write_pi_nacre_context,
)
from long_haul.resource_policy import (
    AuthorityClass,
    PolicyDecisionStatus,
    PolicyRule,
    ResourceAction,
    ResourceIdentity,
    ResourceLifecycle,
    ResourcePolicyEvaluator,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from long_haul.work import (
    AcceptancePredicate,
    PredicateKind,
    WorkBudget,
    WorkContract,
    WorkSession,
)

NOW = datetime(2026, 11, 11, tzinfo=UTC)


def contract() -> WorkContract:
    return WorkContract(
        contract_id="contract-1",
        mission_id="mission-1",
        objective="repair the bounded repository fixture",
        allowed_paths=["fixture"],
        capabilities=["read", "edit", "test"],
        budget=WorkBudget(wall_seconds=60),
        success_predicates=[
            AcceptancePredicate(
                predicate_id="tests",
                kind=PredicateKind.TEST,
                description="fixture tests pass",
                command=["pytest"],
            )
        ],
    )


def session(*, crew_id: str | None = "crew-1") -> WorkSession:
    return WorkSession(
        session_id="session-1",
        contract_id="contract-1",
        crew_id=crew_id,
        execution_plan_id="plan-1",
    )


def policy_receipt():
    profile = ResourcePolicyProfile(
        identity=ResourceIdentity(
            resource_id="urn:nacre:claim:*",
            resource_type="accepted-memory-class",
            content_digest="sha256:class-policy",
        ),
        authority_class=AuthorityClass.ACCEPTED_MEMORY,
        lifecycle=ResourceLifecycle.ACTIVE,
        policy_id="urn:policy:nacre-crew-1",
        policy_assigner="urn:long-haul:operator",
        permissions=[
            PolicyRule(
                rule_id="allow-mission-recall",
                actions=[ResourceAction.AI_AUGMENT],
                purposes=["mission-assistance"],
                principal_ids=["session-1"],
                crew_ids=["crew-1"],
                mission_ids=["mission-1"],
                contract_ids=["contract-1"],
            )
        ],
    )
    request = ResourceUseRequest(
        request_id="policy-request-1",
        resource_id="urn:nacre:claim:*",
        action=ResourceAction.AI_AUGMENT,
        purpose="mission-assistance",
        caller_id="session-1",
        crew_id="crew-1",
        mission_id="mission-1",
        contract_id="contract-1",
        now=NOW,
    )
    decision = ResourcePolicyEvaluator().evaluate(profile, request)
    assert decision.status is PolicyDecisionStatus.ALLOW
    receipt = nacre_policy_receipt(request, decision)
    assert receipt.request_fingerprint == request.fingerprint
    return receipt


def launch_context():
    return build_pi_nacre_context(
        session=session(),
        contract=contract(),
        options=PiNacreLaunchOptions(
            profile_id="crew-profile-1", evidence_ref="event:caller-admitted-1",
            scopes=[PiNacreScope(scope_type="crew", scope_id="crew-1")],
            repository_id="pH34r-pH/long-haul", policy_decisions=[policy_receipt()],
            source_event_refs=["event:mission-admitted-1"], capture_proposals=True,
        ),
    )


def test_build_context_binds_authoritative_work_identity():
    context = launch_context()

    assert context.caller_context.mode == "long-haul-authenticated"
    assert context.caller_context.caller_id == "session-1"
    assert context.caller_context.crew_id == "crew-1"
    assert context.caller_context.mission_id == "mission-1"
    assert context.caller_context.contract_id == "contract-1"
    assert context.caller_context.evidence_ref == "event:caller-admitted-1"
    assert context.scopes == [PiNacreScope(scope_type="crew", scope_id="crew-1")]
    assert context.policy_decisions[0].status == "ALLOW"
    assert context.policy_decisions[0].evaluator_implementation == ResourcePolicyEvaluator.implementation_reference()
    assert context.policy_decisions[0].request_fingerprint.startswith("resource-use-v1:")
    assert json.loads(context.model_dump_json())["policy_decisions"][0]["evaluator_implementation"] == (
        ResourcePolicyEvaluator.implementation_reference())
    assert json.loads(context.model_dump_json())["policy_decisions"][0]["request_fingerprint"] == (
        context.policy_decisions[0].request_fingerprint)
    assert context.capture_proposals is True


def test_policy_receipt_preserves_legacy_unversioned_serialization():
    legacy = policy_receipt().model_copy(update={"evaluator_implementation": None, "request_fingerprint": None})
    assert "evaluator_implementation" not in json.loads(legacy.model_dump_json())
    assert "request_fingerprint" not in json.loads(legacy.model_dump_json())
    assert "evaluator_implementation" in json.loads(policy_receipt().model_dump_json())


def test_context_rejects_missing_crew_or_contract_mismatch():
    with pytest.raises(ValueError, match="crew id"):
        build_pi_nacre_context(
            session=session(crew_id=None),
            contract=contract(),
            options=PiNacreLaunchOptions(
                profile_id="crew-profile-1", evidence_ref="event:caller-admitted-1",
                scopes=[PiNacreScope(scope_type="crew", scope_id="crew-1")],
            ),
        )

    mismatched = session().model_copy(update={"contract_id": "other-contract"})
    with pytest.raises(ValueError, match="do not match"):
        build_pi_nacre_context(
            session=mismatched,
            contract=contract(),
            options=PiNacreLaunchOptions(
                profile_id="crew-profile-1", evidence_ref="event:caller-admitted-1",
                scopes=[PiNacreScope(scope_type="crew", scope_id="crew-1")],
            ),
        )


def test_policy_receipt_rejects_non_augmentation_requests():
    request = ResourceUseRequest(
        request_id="read-request",
        resource_id="urn:nacre:claim:*",
        action=ResourceAction.READ,
        caller_id="session-1",
        now=NOW,
    )
    profile = ResourcePolicyProfile(
        identity=ResourceIdentity(
            resource_id="urn:nacre:claim:*",
            resource_type="accepted-memory-class",
        ),
        lifecycle=ResourceLifecycle.ACTIVE,
        policy_id="urn:policy:read",
        policy_assigner="urn:operator",
        permissions=[PolicyRule(rule_id="read", actions=[ResourceAction.READ])],
    )
    decision = ResourcePolicyEvaluator().evaluate(profile, request)
    with pytest.raises(ValueError, match="AI_AUGMENT"):
        nacre_policy_receipt(request, decision)


def test_policy_receipt_rejects_rebinding_an_allowed_read():
    request = ResourceUseRequest(
        request_id="same-id", resource_id="urn:resource", caller_id="original",
        action=ResourceAction.READ, now=NOW,
    )
    profile = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:resource", resource_type="fixture"),
        lifecycle=ResourceLifecycle.ACTIVE, policy_id="urn:policy", policy_assigner="urn:operator",
        permissions=[PolicyRule(rule_id="read", actions=[ResourceAction.READ])],
    )
    decision = ResourcePolicyEvaluator().evaluate(profile, request)
    forged = request.model_copy(update={
        "action": ResourceAction.AI_AUGMENT, "purpose": "mission-assistance", "caller_id": "attacker",
    })
    with pytest.raises(ValueError, match="binding"):
        nacre_policy_receipt(forged, decision)


def test_owner_only_context_file_round_trips(tmp_path):
    target = tmp_path / "private" / "pi-nacre-context.json"
    written = write_pi_nacre_context(target, launch_context())
    payload = json.loads(written.read_text(encoding="utf-8"))

    assert payload["caller_context"]["crew_id"] == "crew-1"
    assert payload["policy_decisions"][0]["action"] == "ai-augment"
    assert payload["capture_proposals"] is True
    if os.name == "posix":
        assert stat.S_IMODE(written.stat().st_mode) == 0o600
        assert stat.S_IMODE(written.parent.stat().st_mode) == 0o700


def test_context_writer_rejects_relative_or_symlink_targets(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        write_pi_nacre_context(Path("relative.json"), launch_context())

    if os.name == "posix":
        outside = tmp_path / "outside.json"
        target = tmp_path / "context.json"
        target.symlink_to(outside)
        with pytest.raises(PermissionError, match="symlink"):
            write_pi_nacre_context(target, launch_context())
        assert not outside.exists()


def test_environment_contains_only_explicit_paths(tmp_path):
    context_file = tmp_path / "context.json"
    state_root = tmp_path / "state"
    python = tmp_path / "python"
    environment = pi_nacre_environment(
        context_file=context_file,
        state_root=state_root,
        python_executable=python,
    )

    assert environment == {
        "NACRE_RECALL_CALLER_CONTEXT_FILE": str(context_file),
        "NACRE_RECALL_STATE_ROOT": str(state_root),
        "NACRE_RECALL_PYTHON": str(python),
        "NACRE_RECALL_MODULE": "nacre_recall",
    }
