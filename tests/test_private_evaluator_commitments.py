"""Synthetic regression for sealed evaluator identity and disclosure boundaries."""
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from repository_evaluator_fixture import git, profile, setup
from test_repository_evaluator import FixtureSandbox

from long_haul.resource_policy import (
    PolicyRule,
    ResourceIdentity,
    ResourceLifecycle,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from long_haul.work.private_evaluator import (
    PRIVATE_PROFILE_REFERENCE,
    PrivateEvaluatorProfile,
    PrivateEvidenceAdmission,
)
from long_haul.work.repository_candidate import freeze_repository_candidate
from long_haul.work.repository_evaluation_models import work_contract_fingerprint
from long_haul.work.repository_evaluator import (
    RepositoryEvaluationRunner,
    private_repository_evidence_event,
    public_evaluator_reference,
    repository_evidence_event,
)
from long_haul.work.repository_models import DigestReference, content_digest


def synthetic_profile(hidden):
    original = profile(FixtureSandbox.identity)
    checks = []
    for check in original.checks:
        if check.requirement.classification == "SEALED":
            command = check.command.model_copy(update={
                "stdin": f"[{hidden}, 2]\n", "expected_stdout_digest": content_digest(f"{hidden + 2}\n".encode()),
            })
            requirement = check.requirement.model_copy(update={"description": f"private fixture value {hidden}"})
            check = check.model_copy(update={"command": command, "requirement": requirement})
        checks.append(check)
    return original.model_copy(update={"checks": tuple(checks)})


def synthetic_keys(sealed):
    return {"sealed": sealed, "scope": b"scope-only-synthetic-key-material-32", "inspect": b"inspect-only-synthetic-key-material-32"}


def evidence_admission(contract, evaluator, attempt_id, writer):
    policy = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:private-evidence", resource_type="evaluator-evidence",
                                  content_digest=evaluator.fingerprint),
        policy_id="urn:fixture:retention", policy_assigner="urn:fixture:controller", lifecycle="active",
        permissions=[PolicyRule(rule_id="retain", actions=["retain"], purposes=["repository-evaluator-evidence"],
                                principal_ids=[evaluator.evaluator_id], contract_ids=[contract.contract_id])],
    )
    request = ResourceUseRequest(
        request_id="private-evidence:" + attempt_id, resource_id=policy.identity.resource_id,
        action="retain", purpose="repository-evaluator-evidence", caller_id=evaluator.evaluator_id,
        contract_id=contract.contract_id, mission_id=contract.mission_id,
        repository_id=contract.repository_work.source.repository_id, now=datetime.now(UTC) - timedelta(seconds=1),
    )
    return PrivateEvidenceAdmission(policy=policy, expected_policy_fingerprint=policy.fingerprint, request=request,
                                    expected_request_fingerprint=request.fingerprint,
                                    contract_fingerprint=work_contract_fingerprint(contract),
                                    evaluator_fingerprint=evaluator.fingerprint, attempt_id=attempt_id, writer=writer)


def test_sealed_commitment_is_stable_private_and_not_an_enumeration_oracle():
    private = synthetic_profile(4)
    key = b"synthetic-test-only-per-check-key!"
    sealed = PrivateEvaluatorProfile.from_profile(private, synthetic_keys(key))
    replay = PrivateEvaluatorProfile.from_profile(private, synthetic_keys(key))
    assert sealed.fingerprint == replay.fingerprint
    assert sealed.check_fingerprint(private.checks[1]) == replay.check_fingerprint(private.checks[1])
    public = json.dumps(sealed.public_projection()) + sealed.fingerprint + repr(sealed)
    assert "[4, 2]" not in public and "private fixture value 4" not in public
    assert content_digest(b"6\n") not in public and key.hex() not in public
    guessed = {PrivateEvaluatorProfile.from_profile(synthetic_profile(value), synthetic_keys(b"attacker-key-with-32-bytes-minimum")).fingerprint
               for value in range(10)}
    assert sealed.fingerprint not in guessed
    bare_checks = {synthetic_profile(value).checks[1].fingerprint for value in range(10)}
    assert sealed.check_fingerprint(private.checks[1]) not in bare_checks
    assert sealed.fingerprint != PrivateEvaluatorProfile.from_profile(synthetic_profile(5), synthetic_keys(key)).fingerprint
    assert sealed.fingerprint != PrivateEvaluatorProfile.from_profile(private, synthetic_keys(b"different-test-key-at-least-32-bytes")).fingerprint
    assert sealed.check_fingerprint(private.checks[-1]) != private.checks[-1].fingerprint
    assert private.checks[-1].requirement.description not in json.dumps(sealed.public_projection())
    guessed_inspection = {private.checks[-1].model_copy(update={"artifact_digest": content_digest(str(value).encode())}).fingerprint
                          for value in range(10)}
    hidden_inspection = private.checks[-1].model_copy(update={"artifact_digest": content_digest(b"4")})
    inspection_profile = private.model_copy(update={"checks": (*private.checks[:-1], hidden_inspection)})
    keyed_inspection = PrivateEvaluatorProfile.from_profile(inspection_profile, synthetic_keys(key))
    assert hidden_inspection.fingerprint in guessed_inspection
    assert keyed_inspection.check_fingerprint(hidden_inspection) not in guessed_inspection


def test_private_profile_rejects_missing_short_or_extra_keys():
    profile_v1 = synthetic_profile(4)
    for keys in ({}, synthetic_keys(b"short"), {**synthetic_keys(b"x" * 32), "unexpected": b"y" * 32}):
        with pytest.raises(ValueError):
            PrivateEvaluatorProfile.from_profile(profile_v1, keys)


def test_generated_private_profiles_have_fresh_identities():
    profile_v1 = synthetic_profile(4)
    assert PrivateEvaluatorProfile.generate(profile_v1).fingerprint != PrivateEvaluatorProfile.generate(profile_v1).fingerprint


def test_private_profile_rejects_key_reuse_between_sealed_checks():
    profile_v1 = synthetic_profile(4)
    sealed = profile_v1.checks[1]
    second = sealed.model_copy(update={"requirement": sealed.requirement.model_copy(update={"predicate_id": "second"})})
    profile_v1 = profile_v1.model_copy(update={"checks": (*profile_v1.checks, second)})
    with pytest.raises(ValueError):
        PrivateEvaluatorProfile.from_profile(profile_v1, {**synthetic_keys(b"x" * 32), "second": b"x" * 32})


def private_evaluation_fixture(tmp_path):
    root, old_contract, _, old_profile = setup(tmp_path, FixtureSandbox.identity)
    private = PrivateEvaluatorProfile.from_profile(old_profile, synthetic_keys(b"synthetic-only-key-is-at-least-32"))
    extension = old_contract.repository_work.model_copy(update={
        "sealed_evaluator": DigestReference(reference=PRIVATE_PROFILE_REFERENCE, digest=private.fingerprint),
        "public_evaluation": public_evaluator_reference(private),
    })
    checks = {check.requirement.predicate_id: check for check in private.checks}
    predicates = [predicate.model_copy(update={"description": private.public_requirement(checks[predicate.predicate_id])["description"],
        "evaluator_reference": DigestReference(
        reference="urn:long-haul:evaluator-requirement:" + predicate.predicate_id,
        digest=private.check_fingerprint(checks[predicate.predicate_id]),
    )}) for predicate in old_contract.success_predicates]
    contract = old_contract.model_copy(update={"repository_work": extension, "success_predicates": predicates})
    snapshot = freeze_repository_candidate(root, git(root, "rev-parse", "HEAD"), contract)
    runner = RepositoryEvaluationRunner(FixtureSandbox(private.environment))
    return old_profile, private, contract, snapshot, runner


def test_private_evaluation_redacts_hidden_output_and_rejects_raw_public_event(tmp_path):
    old_profile, private, contract, snapshot, runner = private_evaluation_fixture(tmp_path)
    with pytest.raises(TypeError, match="evaluate_private"):
        runner.evaluate(contract, snapshot, private, attempt_id="private-attempt", packet_id="private-packet")
    private_packets = []
    admission = evidence_admission(contract, private, "private-attempt", private_packets.append)
    with pytest.raises(TypeError, match="admitted private evidence sink"):
        runner.evaluate_private(contract, snapshot, private, attempt_id="private-attempt",
                                packet_id="unadmitted", private_sink=private_packets.append)
    summary, result = runner.evaluate_private(contract, snapshot, private, attempt_id="private-attempt",
                                              packet_id="private-packet", private_sink=admission)
    assert result.disposition.value == "ACCEPTED" and len(private_packets) == 1
    with pytest.raises(ValueError, match="private raw"):
        repository_evidence_event(private_packets[0])
    event = private_repository_evidence_event(summary, contract=contract, packet=private_packets[0],
                                              candidate=snapshot.candidate, profile=private)
    with pytest.raises(ValueError, match="verified summary"):
        private_repository_evidence_event(summary.model_copy(update={"attempt_id": "forged"}), contract=contract,
                                          packet=private_packets[0], candidate=snapshot.candidate, profile=private)
    rotated = PrivateEvaluatorProfile.from_profile(old_profile, synthetic_keys(b"other-synthetic-key-at-least-32-bytes"))
    with pytest.raises(ValueError, match="verified summary"):
        private_repository_evidence_event(summary, contract=contract, packet=private_packets[0],
                                          candidate=snapshot.candidate, profile=rotated)
    public = summary.model_dump_json() + event.model_dump_json()
    assert "[-5, 2]" not in public and content_digest(b"-3\n") not in public
    assert private.check_fingerprint(private.checks[1]) in public


def test_private_evidence_admission_fails_closed_on_denial_and_revocation(tmp_path):
    _, private, contract, snapshot, runner = private_evaluation_fixture(tmp_path)
    admission = evidence_admission(contract, private, "private-attempt", lambda _packet: None)

    def failed_private_sink(_packet):
        raise RuntimeError("private storage unavailable")

    with pytest.raises(RuntimeError, match="private storage unavailable"):
        runner.evaluate_private(contract, snapshot, private, attempt_id="failed-storage",
                                packet_id="not-published", private_sink=evidence_admission(
                                    contract, private, "failed-storage", failed_private_sink))

    initial_calls = len(runner.sandbox.calls)
    wrong_request = admission.request.model_copy(update={"contract_id": "other"})
    for invalid in (
        replace(admission, contract_fingerprint="sha256:" + "0" * 64),
        replace(admission, request=wrong_request, expected_request_fingerprint=wrong_request.fingerprint),
        replace(admission, policy=admission.policy.model_copy(update={"lifecycle": ResourceLifecycle.RETRACTED})),
    ):
        with pytest.raises(PermissionError, match="retention admission"):
            runner.evaluate_private(contract, snapshot, private, attempt_id="private-attempt",
                                    packet_id="invalid", private_sink=invalid)
    denied_policy = admission.policy.model_copy(update={"permissions": []})
    denied = replace(admission, policy=denied_policy, expected_policy_fingerprint=denied_policy.fingerprint)
    with pytest.raises(PermissionError, match="not currently allowed"):
        runner.evaluate_private(contract, snapshot, private, attempt_id="private-attempt",
                                packet_id="denied", private_sink=denied)
    assert len(runner.sandbox.calls) == initial_calls

    revoked_packets = []
    revoked = evidence_admission(contract, private, "revoked-during-run", revoked_packets.append)

    class RevokingSandbox(FixtureSandbox):
        def execute(self, target, command):
            result = super().execute(target, command)
            revoked.policy.lifecycle = ResourceLifecycle.RETRACTED
            return result

    with pytest.raises(PermissionError, match="retention admission"):
        RepositoryEvaluationRunner(RevokingSandbox(private.environment)).evaluate_private(
            contract, snapshot, private, attempt_id="revoked-during-run", packet_id="revoked", private_sink=revoked)
    assert revoked_packets == []


def test_private_evaluation_requires_explicit_sink():
    runner = RepositoryEvaluationRunner(FixtureSandbox(synthetic_profile(4).environment))
    private = PrivateEvaluatorProfile.from_profile(synthetic_profile(4), synthetic_keys(b"synthetic-only-key-is-at-least-32"))
    with pytest.raises(TypeError, match="private evidence sink"):
        runner.evaluate_private(None, None, private, attempt_id="a", packet_id="p", private_sink=None)
