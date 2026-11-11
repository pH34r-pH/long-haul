"""Synthetic regression for sealed evaluator identity and disclosure boundaries."""
import json

import pytest
from repository_evaluator_fixture import git, profile, setup
from test_repository_evaluator import FixtureSandbox

from long_haul.work.private_evaluator import (
    PRIVATE_PROFILE_REFERENCE,
    PrivateEvaluatorProfile,
)
from long_haul.work.repository_candidate import freeze_repository_candidate
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


def test_sealed_commitment_is_stable_private_and_not_an_enumeration_oracle():
    private = synthetic_profile(4)
    key = b"synthetic-test-only-per-check-key!"
    sealed = PrivateEvaluatorProfile.from_profile(private, {"sealed": key})
    replay = PrivateEvaluatorProfile.from_profile(private, {"sealed": key})
    assert sealed.fingerprint == replay.fingerprint
    assert sealed.check_fingerprint(private.checks[1]) == replay.check_fingerprint(private.checks[1])
    public = json.dumps(sealed.public_projection()) + sealed.fingerprint + repr(sealed)
    assert "[4, 2]" not in public and "private fixture value 4" not in public
    assert content_digest(b"6\n") not in public and key.hex() not in public
    guessed = {PrivateEvaluatorProfile.from_profile(synthetic_profile(value), {"sealed": b"attacker-key-with-32-bytes-minimum"}).fingerprint
               for value in range(10)}
    assert sealed.fingerprint not in guessed
    bare_checks = {synthetic_profile(value).checks[1].fingerprint for value in range(10)}
    assert sealed.check_fingerprint(private.checks[1]) not in bare_checks
    assert sealed.fingerprint != PrivateEvaluatorProfile.from_profile(synthetic_profile(5), {"sealed": key}).fingerprint
    assert sealed.fingerprint != PrivateEvaluatorProfile.from_profile(private, {"sealed": b"different-test-key-at-least-32-bytes"}).fingerprint


def test_private_profile_rejects_missing_short_or_extra_keys():
    profile_v1 = synthetic_profile(4)
    for keys in ({}, {"sealed": b"short"}, {"sealed": b"x" * 32, "unexpected": b"y" * 32}):
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
        PrivateEvaluatorProfile.from_profile(profile_v1, {"sealed": b"x" * 32, "second": b"x" * 32})


def test_private_evaluation_redacts_hidden_output_and_rejects_raw_public_event(tmp_path):
    root, old_contract, _, old_profile = setup(tmp_path, FixtureSandbox.identity)
    private = PrivateEvaluatorProfile.from_profile(old_profile, {"sealed": b"synthetic-only-key-is-at-least-32"})
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
    with pytest.raises(TypeError, match="evaluate_private"):
        runner.evaluate(contract, snapshot, private, attempt_id="private-attempt", packet_id="private-packet")
    private_packets = []
    summary, result = runner.evaluate_private(contract, snapshot, private, attempt_id="private-attempt",
                                              packet_id="private-packet", private_sink=private_packets.append)
    assert result.disposition.value == "ACCEPTED" and len(private_packets) == 1
    with pytest.raises(ValueError, match="private raw"):
        repository_evidence_event(private_packets[0])
    event = private_repository_evidence_event(summary, contract=contract, packet=private_packets[0],
                                              candidate=snapshot.candidate, profile=private)
    with pytest.raises(ValueError, match="verified summary"):
        private_repository_evidence_event(summary.model_copy(update={"attempt_id": "forged"}), contract=contract,
                                          packet=private_packets[0], candidate=snapshot.candidate, profile=private)
    rotated = PrivateEvaluatorProfile.from_profile(old_profile, {"sealed": b"other-synthetic-key-at-least-32-bytes"})
    with pytest.raises(ValueError, match="verified summary"):
        private_repository_evidence_event(summary, contract=contract, packet=private_packets[0],
                                          candidate=snapshot.candidate, profile=rotated)
    public = summary.model_dump_json() + event.model_dump_json()
    assert "[-5, 2]" not in public and content_digest(b"-3\n") not in public
    assert private.check_fingerprint(private.checks[1]) in public

    def failed_private_sink(_packet):
        raise RuntimeError("private storage unavailable")

    with pytest.raises(RuntimeError, match="private storage unavailable"):
        runner.evaluate_private(contract, snapshot, private, attempt_id="failed-storage",
                                packet_id="not-published", private_sink=failed_private_sink)


def test_private_evaluation_requires_explicit_sink():
    runner = RepositoryEvaluationRunner(FixtureSandbox(synthetic_profile(4).environment))
    private = PrivateEvaluatorProfile.from_profile(synthetic_profile(4), {"sealed": b"synthetic-only-key-is-at-least-32"})
    with pytest.raises(TypeError, match="private evidence sink"):
        runner.evaluate_private(None, None, private, attempt_id="a", packet_id="p", private_sink=None)
