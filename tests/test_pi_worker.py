"""Host interlock/protocol negatives; no actual Pi/sandbox claim from these tests."""
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from datetime import timedelta

import pytest
from test_harness_trajectory import NOW, core, end, start

from long_haul.adapters.harness_trace import TraceAdmissionDenied, TraceArgumentsInvalid
from long_haul.adapters.pi_admission import (
    GUARD_TITLE,
    PiToolAdmission,
    WorkerResourceAdmission,
    regular_contents,
)
from long_haul.adapters.pi_harness import PiCodingHarness
from long_haul.adapters.pi_worker import _Wire
from long_haul.resource_policy import (
    PolicyRule,
    ResourceAction,
    ResourceIdentity,
    ResourceLifecycle,
    ResourcePolicyProfile,
)
from long_haul.work.repository_models import content_digest


def setup_guard(tmp_path, *, actions=None, content=b"before\n"):
    contract, _, attempt, normalizer = core(harness="pi")
    root = tmp_path / "workspace"
    (root / "fixture").mkdir(parents=True)
    (root / "fixture/code.py").write_bytes(content)
    profile = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:file", resource_type="repository-file",
                                  content_digest=content_digest(content)),
        authority_class="repository-reference", lifecycle=ResourceLifecycle.ACTIVE,
        policy_id="urn:fixture:file-policy", policy_assigner="urn:fixture:operator",
        permissions=[PolicyRule(rule_id="file", actions=actions or [ResourceAction.READ, ResourceAction.AI_AUGMENT,
                                                                    ResourceAction.DERIVE],
                                purposes=["repository-work"], crew_ids=["crew"], contract_ids=["contract"])],
    )
    registry = {"fixture/code.py": WorkerResourceAdmission(profile, profile.fingerprint, content_digest(content))}
    guard = PiToolAdmission(normalizer, registry)
    return contract, attempt, normalizer, root, registry, guard


def request(attempt, call=None, *, ui="ui-1"):
    call = start() if call is None else call
    return {"type": "extension_ui_request", "id": ui, "method": "confirm", "title": GUARD_TITLE,
            "message": json.dumps({"attempt_id": attempt.attempt_id, "session_id": attempt.session_id,
                                   "toolCallId": call["toolCallId"], "toolName": call["toolName"], "args": call["args"]})}


def test_native_allow_binds_operation_and_policy_and_never_discloses_arguments(tmp_path):
    _, attempt, _, root, _, guard = setup_guard(tmp_path)
    call = start(call="EXTERNAL_SECRET_CARRIER")
    guard.start(call)
    receipt = guard.authorize(request(attempt, call), root, NOW)
    assert len(receipt.policies) == 2 and receipt.native_id.startswith("hmac-sha256:")
    assert "EXTERNAL_SECRET_CARRIER" not in receipt.model_dump_json()
    assert "fixture/code.py" not in receipt.model_dump_json()
    assert guard.end(end(call="EXTERNAL_SECRET_CARRIER"), root).digest == receipt.fingerprint


@pytest.mark.parametrize("change", ["attempt_id", "session_id", "toolCallId", "toolName", "args"])
def test_guard_cannot_confirm_another_operation(tmp_path, change):
    _, attempt, _, root, _, guard = setup_guard(tmp_path)
    guard.start(start())
    record = request(attempt)
    payload = json.loads(record["message"])
    payload[change] = {"path": "fixture/other.py"} if change == "args" else "another"
    record["message"] = json.dumps(payload)
    with pytest.raises((TraceArgumentsInvalid, TraceAdmissionDenied)):
        guard.authorize(record, root, NOW)
    assert regular_contents(root) == {"fixture/code.py": b"before\n"}


def test_trace_annotation_is_not_native_execution_authority(tmp_path):
    _, attempt, _, root, _, guard = setup_guard(tmp_path, actions=[ResourceAction.ANNOTATE])
    guard.start(start())
    with pytest.raises(TraceAdmissionDenied):
        guard.authorize(request(attempt), root, NOW)
    with pytest.raises(TraceAdmissionDenied):
        guard.end(end(), root)


@pytest.mark.parametrize("mutation", ["contract", "policy", "expired", "content"])
def test_current_mutable_authority_and_source_rechecked_before_any_native_use(tmp_path, mutation):
    contract, attempt, _, root, registry, guard = setup_guard(tmp_path)
    guard.start(start())
    if mutation == "contract":
        contract.allowed_paths.append("outside")
    elif mutation == "policy":
        registry["fixture/code.py"].profile.lifecycle = ResourceLifecycle.RETRACTED
    elif mutation == "expired":
        profile = registry["fixture/code.py"].profile
        profile.permissions[0].valid_until = NOW - timedelta(seconds=1)
        registry["fixture/code.py"] = replace(registry["fixture/code.py"], expected_policy_fingerprint=profile.fingerprint)
    else:
        (root / "fixture/code.py").write_text("changed")
    with pytest.raises(TraceAdmissionDenied):
        guard.authorize(request(attempt), root, NOW)
    assert not guard.receipts


@pytest.mark.parametrize("path", ["fixture/private\u00a0key", "@fixture/code.py", "~/code.py", "fixture/.pi/settings.json",
                                  "/control/launch.json", "/proc/1/fd/1", "fixture/../code.py", "fixture/missing.py"])
def test_ambiguous_control_and_missing_native_targets_decline(tmp_path, path):
    _, attempt, _, root, _, guard = setup_guard(tmp_path)
    call = start(args={"path": path})
    with pytest.raises(ValueError):
        guard.start(call)
        guard.authorize(request(attempt, call), root, NOW)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "ancestor"])
def test_native_filesystem_projection_rejects_links(tmp_path, kind):
    _, attempt, _, root, _, guard = setup_guard(tmp_path)
    target = root / "fixture/code.py"
    if kind == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "outside")
    elif kind == "hardlink":
        os.link(target, root / "fixture/alias.py")
    else:
        target.unlink()
        target.parent.rmdir()
        (root / "fixture").symlink_to(tmp_path, target_is_directory=True)
    guard.start(start())
    with pytest.raises(ValueError):
        guard.authorize(request(attempt), root, NOW)


def test_duplicate_ui_and_completion_before_confirm_fail_closed(tmp_path):
    _, attempt, _, root, _, guard = setup_guard(tmp_path)
    guard.start(start())
    with pytest.raises(TraceAdmissionDenied):
        guard.end(end(), root)
    guard.authorize(request(attempt), root, NOW)
    with pytest.raises(TraceArgumentsInvalid):
        guard.authorize(request(attempt, ui="ui-2"), root, NOW)


def test_unadmitted_native_mutation_invalidates_completion(tmp_path):
    _, attempt, _, root, _, guard = setup_guard(tmp_path)
    guard.start(start())
    guard.authorize(request(attempt), root, NOW)
    (root / "fixture/code.py").write_text("tampered")
    with pytest.raises(TraceAdmissionDenied):
        guard.end(end(), root)


def test_real_adapter_refuses_forged_success_without_interlock_receipt(tmp_path):
    _, attempt, normalizer, root, registry, _ = setup_guard(tmp_path)
    records = iter([start(), end(), {"type": "agent_settled", "aborted": False}])
    harness = PiCodingHarness(normalizer, registry, root, lambda _: None, lambda: next(records), lambda _: "task")
    harness.start(attempt)
    harness._prompted = True
    harness._prompt_acknowledged = True
    harness.poll(NOW, 1)
    harness.poll(NOW, 2)
    assert harness.projector.state.terminal == "policy_denied"


def test_nonreading_process_does_not_block_bounded_rpc_writes():
    process = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(30)"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    wire = _Wire(process, 4096)
    try:
        started = time.monotonic()
        wire.send({"type": "prompt", "message": "x" * 200000})
        for _ in range(3):
            wire.read()
        assert time.monotonic() - started < 1 and wire.outbound
    finally:
        process.kill()
        process.communicate(timeout=2)
        wire.selector.close()


def test_wire_splits_only_lf_and_caps_both_streams():
    process = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(30)"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    wire = _Wire(process, 64)
    try:
        wire.buffer.extend('{"type":"fixture","text":"a\u2028b"}\n'.encode())
        wire._lines()
        assert wire.receive()["text"] == "a\u2028b"
        wire.buffer.extend(b'[]\n')
        with pytest.raises(TypeError):
            wire._lines()
    finally:
        process.kill()
        process.communicate(timeout=2)
        wire.selector.close()
