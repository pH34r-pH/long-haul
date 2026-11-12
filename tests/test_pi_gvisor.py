"""Required hosted gate: actual pinned Pi + deterministic provider, not a real LLM."""
import json
import os
import select
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pi_worker_fixture import (
    PLAN,
    admit_prompt,
    public_evaluator_admission,
    worker_setup,
)
from repository_evaluator_fixture import PROGRAM, environment
from test_private_evaluator_commitments import evidence_admission, summary_admission

from long_haul.adapters.harness_trace import TraceAdmissionDenied, TraceNormalizer
from long_haul.adapters.pi_quota_worker import QuotaPiWorker
from long_haul.adapters.pi_rpc import pi_prompt
from long_haul.adapters.pi_trajectory import PiTrajectoryProjector
from long_haul.adapters.pi_workspace_copy import copy_docker_workspace
from long_haul.adapters.pi_workspace_fence import LocalWorkspaceFence
from long_haul.adapters.pi_workspace_holder import (
    HolderProfile,
    allocate_workspace_holder,
    verify_workspace_holder,
)
from long_haul.adapters.pi_workspace_intent import (
    WorkspaceLeaseIntent,
    load_workspace_intent,
    persist_workspace_intent,
)
from long_haul.adapters.pi_workspace_seed import build_workspace_seed
from long_haul.adapters.pi_workspace_seeding import HolderSeeder
from long_haul.adapters.pi_workspace_view import PausedWorkspaceView
from long_haul.events.store import EventStore
from long_haul.resource_policy import ResourceAction, ResourceLifecycle
from long_haul.work import WorkCheckpoint, WorkSession, render_digest
from long_haul.work.harness import resume_harness_attempt
from long_haul.work.repository_evaluator import (
    RepositoryEvaluationRunner,
    private_repository_evidence_event,
)
from long_haul.work.repository_models import content_digest
from long_haul.work.repository_sandbox import DockerGVisorSandbox, _control_environment
from long_haul.work.worker_candidate import regular_contents

pytestmark = pytest.mark.skipif(os.environ.get("LONG_HAUL_REQUIRE_PI_GVISOR") != "1",
                                reason="actual Pi/gVisor qualification is mandatory in its hosted CI job")


def evidence(result, events):
    print(json.dumps({"qualification": "actual-pi-synthetic-provider-gvisor", "real_model": False,
                      "result": result.model_dump(mode="json"),
                      "events": [event.model_dump(mode="json") for event in events]}))


def test_gvisor_tmpfs_holder_seed_quotas_and_bounded_snapshot(tmp_path):
    """Qualify only the isolated volume experiment, not worker integration."""
    worker, request, _ = worker_setup(tmp_path, PLAN)
    options = "size=1m,nr_inodes=32,mode=0777,nosuid,nodev,noexec"
    intent = WorkspaceLeaseIntent(token=uuid4().hex, attempt_fingerprint=request.normalizer.attempt.fingerprint,
                                  environment_fingerprint=worker.environment.fingerprint,
                                  daemon_id=worker._control("info", "--format", "{{.ID}}").strip(),
                                  volume_options=options)
    recovery_root = (tmp_path / "workspace-recovery").resolve()
    recovery_root.mkdir(mode=0o700)
    persist_workspace_intent(recovery_root, intent)
    volume, guest = intent.volume_name, intent.worker_name
    image, runtime = worker.environment.image, worker.environment.runtime
    mount = "type=volume,src=" + volume + ",dst=/workspace"
    common = ["--runtime=" + runtime, "--network=none", "--read-only", "--user=65534:65534",
              "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=128m",
              "--mount=" + mount, "--entrypoint=/usr/local/bin/node", "--pull=never"]
    script = """const fs=require('fs'); process.chdir('/workspace');
      fs.writeFileSync('fixture/a','changed'); fs.writeFileSync('fixture/new/b','created');
      let bytes=false, inodes=false;
      try { fs.writeFileSync('large', Buffer.alloc(2*1024*1024)); } catch(e) { bytes=e.code==='ENOSPC'; }
      fs.rmSync('large', {force:true});
      for(let i=0;i<128;i++) { try { fs.writeFileSync('bomb'+i,''); } catch(e) { inodes=e.code==='ENOSPC'; break; } }
      for(let i=0;i<128;i++) fs.rmSync('bomb'+i,{force:true});
      if(!bytes || !inodes) process.exit(2); console.log(JSON.stringify({bytes,inodes}));
      setInterval(()=>{},60000);"""
    def labels(role):
        return ["--label=" + key + "=" + value for key, value in intent.labels(role).items()]

    with LocalWorkspaceFence(recovery_root, intent.token, intent.fingerprint).launch() as grant, ExitStack() as cleanup:
        cleanup.callback(grant.cleanup, worker._control)
        scratch = grant.scratch_root()
        (scratch / "synthetic-host-artifact").write_text("fixture")
        holder_profile = HolderProfile(image, worker.image_id,
                                       tuple(key + "=" + value for key, value in worker.image_environment.items()),
                                       worker._verify_runtime)
        holder_id = allocate_workspace_holder(recovery_root, intent.token, intent.fingerprint, worker._control,
                                             holder_profile, grant)
        targets, contents = {"fixture/a", "fixture/new/b"}, {"fixture/a": b"fixture"}
        digest = content_digest(build_workspace_seed(targets, contents))
        seeder = HolderSeeder(recovery_root, intent.token, intent.fingerprint, worker._control, worker.docker_path,
                              holder_id, holder_profile, lambda fingerprint: fingerprint == digest, grant)
        seeder.seed(targets, contents)
        grant.begin_worker()
        guest_id = worker._control("create", "--name", guest, *labels("worker"), *common, image, "-e", script).strip()
        worker._control("start", guest_id)
        deadline = time.monotonic() + 15
        observed = ""
        while not observed and time.monotonic() < deadline:
            observed = worker._control("logs", guest_id).strip()
            if not observed:
                time.sleep(.1)
        assert json.loads(observed) == {"bytes": True, "inodes": True}

        def verify():
            verify_workspace_holder(worker._control, intent, holder_profile, holder_id, running=True)
            guest_info, holder_info = json.loads(worker._control("inspect", guest_id, holder_id))
            for info in (guest_info, holder_info):
                host, config = info["HostConfig"], info["Config"]
                assert info["Image"] == worker.image_id and host["Runtime"] == runtime
                assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"] and config["User"] == "65534:65534"
                assert "ALL" in host["CapDrop"] and "no-new-privileges" in host["SecurityOpt"]
                assert config["Entrypoint"] == ["/usr/local/bin/node"]
            assert guest_info["Config"]["Cmd"] == ["-e", script]
            assert holder_info["Config"]["Cmd"] == ["-e", "setInterval(()=>{},60000)"]

        view = PausedWorkspaceView(worker._control, worker.docker_path, guest_id, holder_id, volume, options, verify)
        with view() as root:
            assert regular_contents(root) == {"fixture/a": b"changed", "fixture/new/b": b"created"}
        worker._control("kill", guest_id)
        assert copy_docker_workspace(worker.docker_path, holder_id, 15) == {
            "fixture/a": b"changed", "fixture/new/b": b"created",
        }
    assert (recovery_root / (intent.token + ".cleanup.json")).is_file()
    assert not scratch.exists()


def test_actual_pi_repair_freezes_after_cleanup_then_independent_evaluator(tmp_path):
    worker, request, profile = worker_setup(tmp_path, PLAN)
    result, events, snapshot = worker.run(request)
    evidence(result, events)
    assert result.terminal == "settled" and result.candidate_disposition == "frozen" and snapshot is not None
    assert result.acceptance == "not_evaluated" and result.cleanup.process_tree_stopped
    assert [entry.status for entry in result.inputs] == ["requested", "acknowledged"]
    assert [event.tool for event in events if event.phase == "end"] == ["read", "edit"]
    assert all(event.execution_admission is not None for event in events if event.phase == "end")
    assert b"a + b" in snapshot.contents["program.py"]
    sandbox = DockerGVisorSandbox(environment(), Path(os.environ["LONG_HAUL_RUNSC"]))
    scratch = tmp_path / "evaluator-scratch"
    scratch.mkdir(mode=0o700)
    packet, evaluation = RepositoryEvaluationRunner(sandbox, scratch_root=scratch).evaluate(
        request.normalizer.contract, snapshot, profile, attempt_id="independent-after-pi", packet_id="independent-after-pi")
    assert evaluation.disposition.value == "ACCEPTED" and packet.validity == "valid"
    assert list(scratch.iterdir()) == []
    print(json.dumps({"qualification": "independent-evaluation-after-actual-pi", "packet": packet.model_dump(mode="json")}))


@contextmanager
def quota_fixture(tmp_path, plan):
    worker, request, profile = worker_setup(tmp_path, plan, seconds=90, worker_type=QuotaPiWorker)
    root = (tmp_path / "quota-recovery").resolve()
    root.mkdir(mode=0o700)
    intent = WorkspaceLeaseIntent.generate(request.normalizer.attempt.fingerprint, worker.environment.fingerprint,
                                           worker._control("info", "--format", "{{.ID}}").strip())
    persist_workspace_intent(root, intent)
    with LocalWorkspaceFence(root, intent.token, intent.fingerprint).launch() as grant, ExitStack() as cleanup:
        cleanup.callback(grant.cleanup, worker._control)
        yield worker, request, profile, grant, root, intent


@pytest.mark.parametrize("diagnostic_sample", range(3))
def test_actual_pi_uses_fenced_quota_workspace_and_reclaims_scratch(tmp_path, monkeypatch, diagnostic_sample):
    with quota_fixture(tmp_path, PLAN) as (worker, request, profile, grant, root, intent):
        original_control = worker._control
        original_end = PiTrajectoryProjector._end

        def synthetic_end(projector, record, *args):
            if record.get("toolName") == "edit" and record.get("isError") is True:
                error = json.dumps(record.get("result")).lower()
                print(json.dumps({"qualification": "synthetic-quota-edit-error", "sample": diagnostic_sample,
                                  "result_type": type(record.get("result")).__name__,
                                  "permission": any(word in error for word in ("eacces", "eperm", "permission")),
                                  "read_only": "read-only" in error or "erofs" in error,
                                  "missing": "enoent" in error or "not found" in error,
                                  "no_match": "could not find" in error or "no match" in error,
                                  "signals": [term for term in ("invalid", "argument", "edits", "oldtext",
                                              "newtext", "schema", "validation", "exact", "unique", "string",
                                              "expected", "content", "operation", "timeout", "abort", "denied",
                                              "failed", "missing", "not found", "unsupported", "write", "read")
                                              if term in error]}))
            return original_end(projector, record, *args)

        monkeypatch.setattr(PiTrajectoryProjector, "_end", synthetic_end)

        def synthetic_control(*arguments):
            if len(arguments) != 3 or arguments[:2] != ("kill", "--signal=KILL"):
                return original_control(*arguments)
            with tempfile.TemporaryDirectory(prefix="lh-synthetic-docker-config-") as config:
                result = subprocess.run([str(worker.docker_path), "--config", config, *arguments],
                                        env=_control_environment(), capture_output=True,
                                        timeout=15, check=False)
            error = result.stderr.lower()
            print(json.dumps({"qualification": "synthetic-quota-stop-diagnostic",
                              "sample": diagnostic_sample,
                              "acknowledged": result.returncode == 0,
                              "mentions_paused": b"paused" in error,
                              "mentions_zombie": b"zombie" in error}))
            result.check_returncode()
            return result.stdout.decode()

        monkeypatch.setattr(worker, "_control", synthetic_control)
        result, events, snapshot = worker.run_bounded(request, grant)
        assert result.terminal == "settled" and snapshot is not None and result.cleanup.process_tree_stopped
        completed = [event for event in events if event.phase == "end"]
        print(json.dumps({"qualification": "synthetic-quota-edit-diagnostic", "sample": diagnostic_sample,
                          "outcomes": [event.outcome.value for event in completed]}))
        assert [event.tool for event in completed] == ["read", "edit"]
        assert all(event.outcome.value == "success" for event in completed)
        assert b"a + b" in snapshot.contents["program.py"] and not worker.lease_releasable
        sandbox = DockerGVisorSandbox(environment(), Path(os.environ["LONG_HAUL_RUNSC"]))
        packet, evaluation = RepositoryEvaluationRunner(sandbox, scratch_root=grant.scratch_root()).evaluate(
            request.normalizer.contract, snapshot, profile, attempt_id="quota-fixture", packet_id="quota-fixture")
        assert packet.validity == "valid" and evaluation.disposition.value == "ACCEPTED"
    assert worker.lease_releasable and not (root / (intent.token + ".scratch")).exists()
    assert (root / (intent.token + ".cleanup.json")).is_file()


def test_actual_quota_pi_cancellation_reclaims_resources_without_candidate(tmp_path):
    plan = {"mode": "hang", "actions": []}
    with quota_fixture(tmp_path, plan) as (worker, request, _, grant, root, intent):
        result, _, snapshot = worker.run_bounded(replace(request, cancel_after=3), grant)
        assert result.terminal == "cancelled" and snapshot is None and result.candidate is None
        assert result.cleanup.process_tree_stopped and not worker.lease_releasable
    assert worker.lease_releasable and not (root / (intent.token + ".scratch")).exists()
    assert (root / (intent.token + ".cleanup.json")).is_file()


def _test_docker_control(config, *arguments):
    result = subprocess.run(["/usr/bin/docker", "--config", config, *arguments], env=_control_environment(),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=True)
    return result.stdout.decode()


def test_synthetic_gvisor_paused_kill_diagnostic(tmp_path):
    """Record provider behavior only; this does not qualify a stop boundary."""
    worker, _, _ = worker_setup(tmp_path, PLAN)
    name = "lh-paused-diagnostic-" + uuid4().hex
    container = None
    try:
        container = worker._control("create", "--name=" + name, "--runtime=long-haul-gvisor", "--network=none",
            "--read-only", "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--entrypoint=/usr/local/bin/node", "--pull=never", worker.environment.image,
            "-e", "setInterval(()=>{},60000)").strip()
        worker._control("start", container)
        worker._control("pause", container)
        with tempfile.TemporaryDirectory(prefix="lh-test-docker-config-") as config:
            result = subprocess.run(["/usr/bin/docker", "--config", config, "kill", "--signal=KILL", container],
                                    env=_control_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    timeout=15, check=False)
        info, = json.loads(worker._control("inspect", container))
        state = info["State"]
        message = result.stderr.decode("utf-8", "replace").replace(container, "<container>").replace(name, "<name>")
        message = "".join(char if 32 <= ord(char) < 127 else " " for char in message[:1024])
        print(json.dumps({"qualification": "synthetic-paused-kill-diagnostic", "returncode": result.returncode,
                          "running": state.get("Running"), "paused": state.get("Paused"), "stderr": message}))
        assert isinstance(state.get("Running"), bool) and isinstance(state.get("Paused"), bool)
    finally:
        if container is not None:
            try:
                worker._control("unpause", container)
            except subprocess.SubprocessError:
                pass  # A successfully killed synthetic guest is not paused.
            worker._control("rm", "--force", container)


def _wait_for_quota_guest(control, name, process):
    deadline = time.monotonic() + 55
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("synthetic controller exited before guest launch")
        try:
            info, = json.loads(control("inspect", name))
            if info["State"].get("Running") is True:
                return info["Id"]
        except subprocess.CalledProcessError:
            pass
        time.sleep(.2)
    raise AssertionError("synthetic quota guest did not start within the test deadline")


def test_crashed_quota_pi_controller_requires_recovery_before_reuse(tmp_path):
    script = ("import json,sys; from pathlib import Path; from test_pi_gvisor import quota_fixture; "
              "p=Path(sys.argv[1]); ctx=quota_fixture(p,{'mode':'hang','actions':[]}); "
              "worker,request,profile,grant,root,intent=ctx.__enter__(); "
              "print(json.dumps({'token':intent.token,'fingerprint':intent.fingerprint}),flush=True); "
              "worker.run_bounded(request,grant)")
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONPATH": str(Path.cwd() / "tests"),
           **{name: os.environ[name] for name in ("LONG_HAUL_RUNSC", "LONG_HAUL_PI_RUNTIME", "LONG_HAUL_PI_RECEIPT")}}
    process = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    root, details, marker = (tmp_path / "quota-recovery").resolve(), None, None
    with tempfile.TemporaryDirectory(prefix="lh-test-docker-config-") as config:
        control = lambda *args: _test_docker_control(config, *args)
        try:
            assert select.select([process.stdout], [], [], 45)[0]
            line = process.stdout.readline()
            if not line:
                raise AssertionError("synthetic controller exited before its recovery intent was ready")
            details = json.loads(line)
            intent = load_workspace_intent(root, details["token"], details["fingerprint"])
            name = intent.worker_name
            guest = _wait_for_quota_guest(control, name, process)
            time.sleep(1)
            assert process.poll() is None
            scratch = root / (details["token"] + ".scratch")
            assert (scratch / "pi-projection" / "program.py").is_file()
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
            if details is None and root.exists():
                candidates = [path for path in root.glob("*.json") if len(path.stem) == 32]
                if len(candidates) == 1:
                    intent = WorkspaceLeaseIntent.model_validate_json(candidates[0].read_bytes())
                    details = {"token": intent.token, "fingerprint": intent.fingerprint}
            if details is not None and (root / "active.json").exists():
                marker = LocalWorkspaceFence(root, details["token"], details["fingerprint"]).recover(control)
        assert marker is not None and marker.is_file() and not scratch.exists()
        assert not control("container", "ls", "--all", "--no-trunc", "--filter", "id=" + guest, "--format", "{{.ID}}").strip()


def test_actual_pi_keyed_evaluator_has_no_sealed_output_in_public_event(tmp_path):
    worker, request, profile = worker_setup(tmp_path, PLAN, private=True)
    result, _, snapshot = worker.run(request)
    assert result.terminal == "settled" and snapshot is not None
    packets = []
    contract = request.normalizer.contract
    admission = evidence_admission(contract, profile, "private-fixture", packets.append)
    sandbox = DockerGVisorSandbox(environment(), Path(os.environ["LONG_HAUL_RUNSC"]))
    summary, evaluation = RepositoryEvaluationRunner(sandbox).evaluate_private(
        contract, snapshot, profile, attempt_id="private-fixture", packet_id="private-fixture", private_sink=admission)
    assert evaluation.disposition.value == "ACCEPTED" and len(packets) == 1
    event = private_repository_evidence_event(summary, contract=contract, packet=packets[0],
                                              candidate=snapshot.candidate, profile=profile,
                                              admission=summary_admission(contract, profile, summary))
    public = event.model_dump_json()
    assert "[-5, 2]" not in public and content_digest(b"-3\n") not in public
    print(json.dumps({"qualification": "actual-pi-keyed-synthetic", "summary": summary.model_dump(mode="json")}))


def test_actual_pi_rejects_missing_evaluator_admission_before_launch(tmp_path):
    worker, request, _ = worker_setup(tmp_path, PLAN)
    with pytest.raises(TypeError, match="trusted worker admission"):
        worker.run(replace(request, evaluator_admission=None))
    assert worker.lease_releasable and worker.pending_recovery is None


def test_actual_pi_write_read_edit_keeps_derived_policy_and_operation_identity(tmp_path):
    plan = {"mode": "success", "actions": [
        {"tool": "write", "args": {"path": "program.py", "content": PROGRAM.decode()}},
        {"tool": "read", "args": {"path": "program.py"}},
        {"tool": "edit", "args": {"path": "program.py", "edits": [{"oldText": "a + b", "newText": "sum((a, b))"}]}},
    ]}
    worker, request, _ = worker_setup(tmp_path, plan)
    result, events, snapshot = worker.run(request)
    evidence(result, events)
    assert result.terminal == "settled" and snapshot is not None
    assert [event.tool for event in events if event.phase == "end"] == ["write", "read", "edit"]
    assert all(event.execution_admission is not None for event in events if event.phase == "end")
    assert b"sum((a, b))" in snapshot.contents["program.py"]
    assert worker.last_derived_bindings["program.py"].identity.content_digest == snapshot.candidate.files[1].digest


@pytest.mark.parametrize("plan,expected", [
    ({"mode": "success", "actions": [{"tool": "write", "args": {"path": "/control/launch.json", "content": "tamper"}}]}, "protocol_error"),
    ({"mode": "success", "actions": [{"tool": "write", "args": {"path": "/proc/1/fd/1", "content": "forged"}}]}, "protocol_error"),
    ({"mode": "provider_failure", "actions": []}, "provider_failure"),
    ({"mode": "hang", "actions": []}, "timed_out"),
])
def test_real_pi_denial_failure_and_deadline_discard_candidates(tmp_path, plan, expected):
    worker, request, _ = worker_setup(tmp_path, plan, seconds=8)
    result, events, snapshot = worker.run(request)
    evidence(result, events)
    assert result.terminal == expected and snapshot is None and result.candidate is None
    assert result.cleanup.process_tree_stopped and result.acceptance == "not_evaluated"


def test_actual_pi_cancel_cleans_process_tree_before_any_candidate(tmp_path):
    worker, request, _ = worker_setup(tmp_path, {"mode": "hang", "actions": []}, seconds=10)
    result, events, snapshot = worker.run(replace(request, cancel_after=3))
    evidence(result, events)
    assert result.terminal == "cancelled" and snapshot is None and result.cleanup.process_tree_stopped


def test_native_policy_denial_precedes_filesystem_change(tmp_path):
    plan = {"mode": "success", "actions": [{"tool": "write", "args": {"path": "program.py", "content": "tamper"}}]}
    worker, request, _ = worker_setup(tmp_path, plan)
    binding = request.registry["program.py"]
    binding.profile.permissions[0].actions = [ResourceAction.READ, ResourceAction.AI_AUGMENT]
    request.registry["program.py"] = replace(binding, expected_policy_fingerprint=binding.profile.fingerprint)
    result, events, snapshot = worker.run(request)
    evidence(result, events)
    assert result.terminal == "policy_denied" and snapshot is None
    assert all(event.phase != "end" or event.summary == "interrupted" for event in events)


def test_revoked_projection_is_denied_before_worker_launch(tmp_path):
    worker, request, _ = worker_setup(tmp_path, PLAN)
    request.registry["program.py"].profile.lifecycle = ResourceLifecycle.RETRACTED
    with pytest.raises(TraceAdmissionDenied):
        worker.run(request)
    assert worker.lease_releasable and worker.pending_recovery is None


def test_actual_pi_resume_starts_fresh_native_session_and_retains_candidate_history(tmp_path):
    worker, request, evaluator = worker_setup(tmp_path, PLAN)
    store = EventStore(tmp_path / "history.jsonl")
    previous, events, snapshot = worker.run(replace(request, event_store=store))
    first_native = worker.last_native_session_tag
    assert previous.terminal == "settled" and snapshot is not None
    old = previous.attempt
    session = WorkSession(session_id=old.session_id, crew_id=old.crew_id, contract_id=old.contract_id)
    checkpoint = WorkCheckpoint(contract_id=old.contract_id, through_event_id=events[-1].event().id)
    digest = render_digest(checkpoint)
    contract = request.normalizer.contract
    digest.context_manifest = contract.repository_work.context_manifest
    plan = {"mode": "success", "actions": [{"tool": "read", "args": {"path": "program.py"}}]}
    attempt, fresh = resume_harness_attempt(previous, session, checkpoint, digest, contract,
                                            worker.harness_identity(plan), "resumed-real-pi")
    trace = request.normalizer.admission
    trace = replace(trace, request=trace.request.model_copy(update={"caller_id": fresh.session_id,
                                                                  "now": datetime.now(UTC)}))
    normalizer = TraceNormalizer(contract, attempt, request.normalizer.policy,
                                 b"synthetic-public-qualification-only-key-256-bits", trace)
    resource = request.registry["program.py"]
    registry = {"program.py": replace(resource, derived=worker.last_derived_bindings["program.py"])}
    prompt = lambda now: pi_prompt(contract, digest, session=fresh, evaluation_time=now)
    resumed, new_events, candidate = worker.run(replace(request, base=snapshot, normalizer=normalizer,
        registry=registry, prompt=prompt, synthetic=plan, event_store=store,
        evaluator_admission=public_evaluator_admission(evaluator, contract, attempt, datetime.now(UTC)),
        prompt_admission=admit_prompt(normalizer, prompt(datetime.now(UTC)))))
    evidence(resumed, new_events)
    assert resumed.terminal == "settled" and candidate.candidate == snapshot.candidate
    assert resumed.attempt.session_id != old.session_id and worker.last_native_session_tag != first_native
    assert resumed.attempt.previous_attempt.digest == previous.fingerprint and resumed.attempt.checkpoint
    assert len(store.events()) == len(events) + len(new_events) + len(previous.inputs) + len(resumed.inputs) + 1
    assert "a + b" not in store.export_jsonl()
