"""Host interlock/protocol negatives; no actual Pi/sandbox claim from these tests."""
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_harness_trajectory import NOW, core, end, start

from long_haul.adapters.harness_trace import TraceAdmissionDenied, TraceArgumentsInvalid
from long_haul.adapters.pi_admission import (
    GUARD_TITLE,
    PiToolAdmission,
    WorkerPolicyBinding,
    WorkerResourceAdmission,
    regular_contents,
)
from long_haul.adapters.pi_harness import PiCodingHarness
from long_haul.adapters.pi_worker import DockerGVisorPiWorker, _Wire
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
    output = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:model-output", resource_type="output-authority"),
        authority_class="authoritative-control", lifecycle="active", policy_id="urn:fixture:output-policy",
        policy_assigner="urn:fixture:operator", permissions=[PolicyRule(rule_id="output", actions=["read", "ai-augment", "derive"],
            purposes=["repository-work"], crew_ids=["crew"], contract_ids=["contract"])],
    )
    registry = {"fixture/code.py": WorkerResourceAdmission(profile, profile.fingerprint, content_digest(content),
        output_policy=WorkerPolicyBinding(output, output.fingerprint))}
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


@pytest.mark.parametrize("actions", [[ResourceAction.DERIVE], [ResourceAction.READ], [ResourceAction.AI_AUGMENT]])
def test_worker_projection_never_copies_existing_bytes_without_read_and_model_admission(tmp_path, actions):
    _, _, normalizer, root, registry, _ = setup_guard(tmp_path, actions=actions)
    projected = tmp_path / "projected"
    projected.mkdir()
    base = SimpleNamespace(contents={"fixture/code.py": (root / "fixture/code.py").read_bytes()})
    with pytest.raises(TraceAdmissionDenied):
        DockerGVisorPiWorker._project(projected, base, registry, normalizer, NOW)
    assert not (projected / "fixture/code.py").exists()


def test_worker_projection_admits_current_read_and_model_policy(tmp_path):
    _, _, normalizer, root, registry, _ = setup_guard(tmp_path)
    projected = tmp_path / "projected"
    projected.mkdir()
    base = SimpleNamespace(contents={"fixture/code.py": (root / "fixture/code.py").read_bytes()})
    DockerGVisorPiWorker._project(projected, base, registry, normalizer, NOW)
    assert (projected / "fixture/code.py").read_bytes() == b"before\n"


def test_worker_projection_rejects_oversized_file_before_copy(tmp_path):
    content = b"x" * (1048576 + 1)
    _, _, normalizer, _, registry, _ = setup_guard(tmp_path, content=content)
    projected = tmp_path / "projected"
    projected.mkdir()
    with pytest.raises(ValueError, match="bounded file size"):
        DockerGVisorPiWorker._project(projected, SimpleNamespace(contents={"fixture/code.py": content}),
                                     registry, normalizer, NOW)
    assert list(projected.iterdir()) == []


def test_worker_projection_rejects_oversized_total_before_copy(tmp_path):
    content = b"x" * 1048576
    _, _, normalizer, _, registry, _ = setup_guard(tmp_path, content=content)
    first = registry.pop("fixture/code.py")
    registry.update({f"fixture/{index}.py": first for index in range(5)})
    projected = tmp_path / "projected"
    projected.mkdir()
    with pytest.raises(ValueError, match="bounded total size"):
        DockerGVisorPiWorker._project(projected, SimpleNamespace(contents={path: content for path in registry}),
                                     registry, normalizer, NOW)
    assert list(projected.iterdir()) == []


def test_worker_projection_checks_all_files_before_copy_and_rejects_policy_drift(tmp_path):
    _, _, normalizer, _root, registry, _ = setup_guard(tmp_path)
    first = registry["fixture/code.py"]
    denied = first.profile.model_copy(deep=True)
    denied.permissions[0].actions = [ResourceAction.DERIVE]
    registry["fixture/denied.py"] = replace(first, profile=denied, expected_policy_fingerprint=denied.fingerprint)
    base = SimpleNamespace(contents={"fixture/code.py": b"before\n", "fixture/denied.py": b"before\n"})
    projected = tmp_path / "projected"
    projected.mkdir()
    with pytest.raises(TraceAdmissionDenied):
        DockerGVisorPiWorker._project(projected, base, registry, normalizer, NOW)
    assert list(projected.iterdir()) == []
    del registry["fixture/denied.py"]
    first.profile.lifecycle = ResourceLifecycle.RETRACTED
    with pytest.raises(TraceAdmissionDenied):
        DockerGVisorPiWorker._project(projected, base, registry, normalizer, NOW)
    assert list(projected.iterdir()) == []


def test_worker_projection_copies_the_exact_checked_bytes(tmp_path):
    _, _, normalizer, _root, registry, _ = setup_guard(tmp_path)

    class ChangedAfterRead(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            self[key] = b"unadmitted replacement"
            return value

    base = SimpleNamespace(contents=ChangedAfterRead({"fixture/code.py": b"before\n"}))
    projected = tmp_path / "projected"
    projected.mkdir()
    DockerGVisorPiWorker._project(projected, base, registry, normalizer, NOW)
    assert (projected / "fixture/code.py").read_bytes() == b"before\n"


def test_worker_rejects_directory_flood_before_copy_or_candidate_scan(tmp_path):
    _, _, normalizer, root, registry, _ = setup_guard(tmp_path)
    projected = tmp_path / "projected"
    projected.mkdir()
    source = registry["fixture/code.py"]
    registry.update({f"fixture/d{index}/code.py": source for index in range(256)})
    base = SimpleNamespace(contents={"fixture/code.py": b"before\n"})
    with pytest.raises(ValueError, match="bounded target count"):
        DockerGVisorPiWorker._project(projected, base, registry, normalizer, NOW)
    assert list(projected.iterdir()) == []
    for index in range(256):
        (root / f"empty-{index}").mkdir()
    with pytest.raises(ValueError, match="bounded directory count"):
        regular_contents(root)


def test_worker_rejects_excessive_directory_depth_before_copy(tmp_path):
    _, _, normalizer, _, registry, _ = setup_guard(tmp_path)
    projected = tmp_path / "projected"
    projected.mkdir()
    registry["fixture/" + "d/" * 64 + "code.py"] = registry["fixture/code.py"]
    with pytest.raises(ValueError, match="bounded directory depth"):
        DockerGVisorPiWorker._project(projected, SimpleNamespace(contents={}), registry, normalizer, NOW)
    assert list(projected.iterdir()) == []


def test_worker_projection_rejects_excessive_distinct_directories(tmp_path):
    _, _, normalizer, _, registry, _ = setup_guard(tmp_path)
    projected = tmp_path / "projected"
    projected.mkdir()
    for index in range(5):
        registry[f"fixture/tree{index}/" + "d/" * 60 + "code.py"] = registry["fixture/code.py"]
    with pytest.raises(ValueError, match="bounded directory count"):
        DockerGVisorPiWorker._project(projected, SimpleNamespace(contents={}), registry, normalizer, NOW)
    assert list(projected.iterdir()) == []


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


@pytest.mark.parametrize("tool,args", [
    ("write", {"path": "fixture/code.py", "content": "allowed\n"}),
    ("edit", {"path": "fixture/code.py", "edits": [{"oldText": "before", "newText": "allowed"}]}),
])
def test_mutation_must_match_exact_authorized_tool_arguments(tmp_path, tool, args):
    _, attempt, _, root, _, guard = setup_guard(tmp_path)
    call = start("mutation", tool, args)
    guard.start(call)
    guard.authorize(request(attempt, call), root, NOW)
    (root / "fixture/code.py").write_bytes(b"unauthorized\n")
    with pytest.raises(TraceAdmissionDenied):
        guard.end(end("mutation", tool), root, NOW)
    assert not guard.derivations


@pytest.mark.parametrize("content", [b"before\nbefore\n", b"before\r\n", b"before\xff"])
def test_edit_rejects_ambiguous_or_noncanonical_input_before_authorization(tmp_path, content):
    _, attempt, _, root, _, guard = setup_guard(tmp_path, content=content)
    call = start("edit", "edit", {"path": "fixture/code.py", "edits": [{"oldText": "before", "newText": "after"}]})
    guard.start(call)
    with pytest.raises(TraceArgumentsInvalid):
        guard.authorize(request(attempt, call), root, NOW)
    assert not guard.receipts and (root / "fixture/code.py").read_bytes() == content


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


@pytest.mark.skipif(os.name != "posix", reason="worker filesystem inspection requires POSIX metadata")
def test_native_interlocks_use_executor_owned_workspace_views(tmp_path):
    _, attempt, normalizer, root, registry, _ = setup_guard(tmp_path)
    view = tmp_path / "trusted-snapshot"
    (view / "fixture").mkdir(parents=True)
    (view / "fixture/code.py").write_bytes(b"before\n")
    (root / "fixture/code.py").write_bytes(b"untrusted-live-content")
    lifecycle, sent = [], []

    @contextmanager
    def workspace_view():
        lifecycle.append("enter")
        try:
            yield view
        finally:
            lifecycle.append("exit")

    records = iter([start(), request(attempt), end()])
    harness = PiCodingHarness(normalizer, registry, root, sent.append, lambda: next(records), lambda _: "task")
    harness.workspace_view = workspace_view
    harness.start(attempt)
    harness._native_session = "synthetic"
    harness._prompt_acknowledged = True
    for tick in range(1, 4):
        harness.poll(NOW, tick)
    assert harness.projector.state.terminal is None
    assert sent[-1]["confirmed"] is True
    assert lifecycle == ["enter", "exit"] * 3


def test_workspace_view_failure_denies_native_confirmation(tmp_path):
    _, attempt, normalizer, root, registry, _ = setup_guard(tmp_path)
    sent = []
    harness = PiCodingHarness(normalizer, registry, root, sent.append, lambda: request(attempt), lambda _: "task")

    @contextmanager
    def failed_view():
        raise ValueError("snapshot unavailable")
        yield  # pragma: no cover

    harness.workspace_view = failed_view
    harness.start(attempt)
    harness._native_session = "synthetic"
    harness._prompt_acknowledged = True
    harness.poll(NOW, 1)
    assert harness.projector.state.terminal == "protocol_error"
    assert not any(item.get("confirmed") is True for item in sent)


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


@pytest.mark.parametrize("program,maximum,expected", [
    ("import sys,time;print('malformed-json',flush=True);time.sleep(30)", 4096, "protocol_error"),
    ("import sys,time;print('x'*4096,flush=True);time.sleep(30)", 64, "resource_exhausted"),
    ("import time;time.sleep(30)", 4096, "timed_out"),
])
def test_executor_classifies_malformed_output_limits_and_nonreader_deadline(tmp_path, program, maximum, expected):
    _, _, normalizer, root, registry, _ = setup_guard(tmp_path)
    worker = object.__new__(DockerGVisorPiWorker)
    worker.environment = SimpleNamespace(limits=SimpleNamespace(max_output_bytes=maximum, wall_seconds=.2))
    process = subprocess.Popen([sys.executable, "-I", "-c", program],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        harness = worker._drive(process, normalizer, registry, root, lambda _: "public fixture", None)
        assert harness.projector.state.terminal == expected
    finally:
        process.kill()
        process.communicate(timeout=2)
