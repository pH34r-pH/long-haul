"""Derived bytes keep every current parent restriction through shared intersection."""
import os
from dataclasses import replace
from datetime import timedelta

import pytest
from test_harness_trajectory import NOW, end, start
from test_pi_worker import request, setup_guard

from long_haul.adapters.harness_trace import TraceAdmissionDenied
from long_haul.adapters.pi_admission import (
    PiToolAdmission,
    WorkerPolicyBinding,
    WorkerPromptAdmission,
)
from long_haul.resource_policy import ResourceLifecycle
from long_haul.work.repository_models import content_digest


def mutate(guard, attempt, root, *, call="write-1", content="after\n", tool="write"):
    args = {"path": "fixture/code.py", "content": content} if tool == "write" else {
        "path": "fixture/code.py", "edits": [{"oldText": "after", "newText": "edited"}]}
    operation = start(call, tool, args)
    guard.start(operation)
    allowed = guard.authorize(request(attempt, operation, ui=call), root, NOW)
    (root / "fixture/code.py").write_text(content)
    completed = guard.end(end(call, tool), root, NOW)
    return allowed, completed


def test_write_read_edit_uses_new_identity_and_retains_exact_parent_policies(tmp_path):
    _, attempt, _, root, registry, guard = setup_guard(tmp_path)
    original = registry["fixture/code.py"].profile.model_dump_json()
    allowed, completed = mutate(guard, attempt, root)
    derived = guard.derived["fixture/code.py"]
    assert derived.identity.resource_id != registry["fixture/code.py"].profile.identity.resource_id
    assert derived.identity.content_digest == content_digest(b"after\n")
    assert derived.operation.digest == allowed.fingerprint and completed.reference == "artifact:worker-derivation"
    read = start("read-derived")
    guard.start(read)
    receipt = guard.authorize(request(attempt, read, ui="read-derived"), root, NOW)
    assert receipt.resource_fingerprint != allowed.resource_fingerprint
    guard.end(end("read-derived"), root, NOW)
    mutate(guard, attempt, root, call="edit-derived", content="edited\n", tool="edit")
    assert guard.derived["fixture/code.py"].identity.content_digest == content_digest(b"edited\n")
    assert len(guard.derivations) == 2 and registry["fixture/code.py"].profile.model_dump_json() == original


def test_conflicting_input_policy_for_same_resource_is_not_overwritten(tmp_path):
    _, attempt, normalizer, root, registry, _ = setup_guard(tmp_path)
    first = registry["fixture/code.py"]
    restricted = first.profile.model_copy(deep=True)
    restricted.permissions[0].actions = [action for action in restricted.permissions[0].actions if action.value != "derive"]
    registry["fixture/code.py"] = replace(first, profile=restricted, expected_policy_fingerprint=restricted.fingerprint)
    (root / "fixture/second.py").write_bytes(b"before\n")
    registry["fixture/second.py"] = first
    guard = PiToolAdmission(normalizer, registry)
    first_read = start("first-read")
    guard.start(first_read)
    guard.authorize(request(attempt, first_read, ui="first-read"), root, NOW)
    guard.end(end("first-read"), root, NOW)
    second_read = start("second-read", args={"path": "fixture/second.py"})
    guard.start(second_read)
    with pytest.raises(TraceAdmissionDenied, match="conflicting parent identity"):
        guard.authorize(request(attempt, second_read, ui="second-read"), root, NOW)
    assert guard._inputs[restricted.identity.resource_id].expected_policy_fingerprint == restricted.fingerprint


def test_conflicting_prompt_policy_for_same_resource_is_not_overwritten(tmp_path):
    _, attempt, _, _, registry, guard = setup_guard(tmp_path)
    original = registry["fixture/code.py"].profile
    restricted = original.model_copy(deep=True)
    restricted.permissions[0].actions = [action for action in restricted.permissions[0].actions if action.value != "derive"]
    def admitted(profile):
        return WorkerPromptAdmission((WorkerPolicyBinding(profile, profile.fingerprint),), "fingerprint",
                                     attempt.context_manifest, attempt.digest)

    guard.admit_prompt(admitted(restricted), "fingerprint", NOW)
    guard.admit_prompt(admitted(restricted), "fingerprint", NOW)
    with pytest.raises(TraceAdmissionDenied, match="conflicting parent identity"):
        guard.admit_prompt(admitted(original), "fingerprint", NOW)
    assert guard._inputs[restricted.identity.resource_id].expected_policy_fingerprint == restricted.fingerprint


def test_final_projection_rejects_direct_mutation_and_revoked_derivation(tmp_path):
    _, attempt, _, root, registry, guard = setup_guard(tmp_path)
    original = {"fixture/code.py": b"before\n"}
    (root / "fixture/code.py").write_text("forged\n")
    with pytest.raises(TraceAdmissionDenied):
        guard.validate_final(root, original, NOW)
    (root / "fixture/code.py").write_bytes(original["fixture/code.py"])
    mutate(guard, attempt, root)
    guard.validate_final(root, original, NOW)
    (root / "fixture/code.py").write_text("forged again\n")
    with pytest.raises(TraceAdmissionDenied):
        guard.validate_final(root, original, NOW)
    (root / "fixture/code.py").write_text("after\n")
    registry["fixture/code.py"].output_policy.profile.lifecycle = ResourceLifecycle.RETRACTED
    with pytest.raises(TraceAdmissionDenied):
        guard.validate_final(root, original, NOW)


@pytest.mark.parametrize("change", ["new", "delete", "pending"])
def test_final_projection_rejects_unadmitted_changes_and_incomplete_calls(tmp_path, change):
    _, _, _, root, _, guard = setup_guard(tmp_path)
    initial = {"fixture/code.py": b"before\n"}
    if change == "new":
        (root / "fixture/other.py").write_text("injected")
    elif change == "delete":
        (root / "fixture/code.py").unlink()
    else:
        guard.start(start())
    with pytest.raises(TraceAdmissionDenied):
        guard.validate_final(root, initial, NOW)


@pytest.mark.parametrize("which", ["source", "output"])
def test_mid_call_revocation_cannot_mint_a_derived_binding(tmp_path, which):
    _, attempt, _, root, registry, guard = setup_guard(tmp_path)
    call = start("write", "write", {"path": "fixture/code.py", "content": "after"})
    guard.start(call)
    guard.authorize(request(attempt, call), root, NOW)
    (root / "fixture/code.py").write_text("after")
    binding = registry["fixture/code.py"]
    (binding.profile if which == "source" else binding.output_policy.profile).lifecycle = ResourceLifecycle.RETRACTED
    with pytest.raises(TraceAdmissionDenied):
        guard.end(end("write", "write"), root, NOW)
    assert not guard.derived and not guard.derivations


@pytest.mark.parametrize("restriction", ["missing", "denied", "purpose", "expiry", "retention"])
def test_output_derivation_denial_precedes_mutation_and_later_use_inherits_limits(tmp_path, restriction):
    _, attempt, normalizer, root, registry, _ = setup_guard(tmp_path)
    binding = registry["fixture/code.py"]
    output = binding.output_policy
    if restriction == "missing":
        registry["fixture/code.py"] = replace(binding, output_policy=None)
    else:
        if restriction == "denied":
            output.profile.permissions[0].actions = []
        elif restriction == "purpose":
            output.profile.privacy.purposes = ["another-purpose"]
        elif restriction == "expiry":
            output.profile.permissions[0].valid_until = NOW - timedelta(seconds=1)
        else:
            output.profile.privacy.retention_until = NOW - timedelta(seconds=1)
        registry["fixture/code.py"] = replace(binding, output_policy=replace(output,
            expected_policy_fingerprint=output.profile.fingerprint))
    guard = PiToolAdmission(normalizer, registry)
    call = start("write", "write", {"path": "fixture/code.py", "content": "after"})
    guard.start(call)
    with pytest.raises(TraceAdmissionDenied):
        guard.authorize(request(attempt, call), root, NOW)
    assert (root / "fixture/code.py").read_bytes() == b"before\n"


def test_derived_read_rechecks_parent_retention(tmp_path):
    _, attempt, _, root, registry, guard = setup_guard(tmp_path)
    output = registry["fixture/code.py"].output_policy.profile
    output.privacy.retention_until = NOW + timedelta(seconds=1)
    binding = registry["fixture/code.py"]
    registry["fixture/code.py"] = replace(binding, output_policy=replace(binding.output_policy,
        expected_policy_fingerprint=output.fingerprint))
    guard = PiToolAdmission(guard.normalizer, registry)
    mutate(guard, attempt, root)
    read = start("later")
    guard.start(read)
    with pytest.raises(TraceAdmissionDenied):
        guard.authorize(request(attempt, read, ui="later"), root, NOW + timedelta(seconds=2))


@pytest.mark.skipif(os.name != "posix", reason="worker filesystem inspection requires POSIX metadata")
def test_confirmation_rechecks_output_expiry_after_workspace_capture(tmp_path):
    _, attempt, normalizer, root, registry, _ = setup_guard(tmp_path)
    binding = registry["fixture/code.py"]
    output = binding.output_policy
    output.profile.privacy.retention_until = NOW + timedelta(seconds=1)
    registry["fixture/code.py"] = replace(binding, output_policy=replace(output,
        expected_policy_fingerprint=output.profile.fingerprint))
    guard = PiToolAdmission(normalizer, registry)
    call = start("write", "write", {"path": "fixture/code.py", "content": "after"})
    guard.start(call)
    guard.authorize(request(attempt, call), root, NOW)
    guard.revalidate_confirmation(NOW)
    with pytest.raises(TraceAdmissionDenied):
        guard.revalidate_confirmation(NOW + timedelta(seconds=2))
