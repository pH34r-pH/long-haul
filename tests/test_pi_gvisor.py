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

from long_haul.adapters import pi_quota_worker
from long_haul.adapters.harness_trace import TraceAdmissionDenied, TraceNormalizer
from long_haul.adapters.pi_harness import PiCodingHarness
from long_haul.adapters.pi_quota_worker import QuotaPiWorker
from long_haul.adapters.pi_rpc import pi_prompt
from long_haul.adapters.pi_trajectory import PiTrajectoryProjector
from long_haul.adapters.pi_worker import _Wire
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


def _wait_synthetic_clock_advance(worker, holder):
    """Observe only the synthetic clock while its writer is still active."""
    deadline = time.monotonic() + 5
    first = None
    while time.monotonic() < deadline:
        value = worker._control("exec", holder, "/usr/local/bin/node", "-e",
                                "process.stdout.write(require('fs').readFileSync('/workspace/fixture/clock'))").strip()
        if len(value) == 8 and value.isascii() and value.isdecimal():
            if first is None:
                first = int(value)
            elif int(value) > first:
                return
        time.sleep(.1)
    raise AssertionError("synthetic guest clock did not advance")


def _qualify_synthetic_holder_view(view, worker, guest, holder):
    _wait_synthetic_clock_advance(worker, holder)
    with view() as root:
        contents = regular_contents(root)
        paused_clock = contents.pop("fixture/clock")
        assert paused_clock
        assert contents == {"fixture/a": b"changed", "fixture/new/b": b"created"}
        time.sleep(.5)
        assert copy_docker_workspace(worker.docker_path, holder, 15)["fixture/clock"] == paused_clock
    if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted":
        captured = _hosted_frozen_holder_capture(worker, guest, holder)
    else:
        worker._control("kill", guest)
        captured = copy_docker_workspace(worker.docker_path, holder, 15)
    assert captured.pop("fixture/clock")
    assert captured == {"fixture/a": b"changed", "fixture/new/b": b"created"}


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
      if(!bytes || !inodes) process.exit(2);
      let tick=0; const pulse=()=>fs.writeFileSync('fixture/clock',String(tick++).padStart(8,'0'));
      pulse(); console.log(JSON.stringify({bytes,inodes})); setInterval(pulse,50);"""
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
        _qualify_synthetic_holder_view(view, worker, guest_id, holder_id)
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
    _record_synthetic_admission_latency(monkeypatch, str(diagnostic_sample))
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


def _record_synthetic_admission_latency(monkeypatch, sample):
    original = PiCodingHarness._confirm
    original_read = _Wire.read
    first_seen = {}

    def measured_read(wire):
        original_read(wire)
        for record in wire.records:
            if record.get("type") == "extension_ui_request":
                first_seen.setdefault(record.get("id"), time.monotonic())

    def measured(harness, record, now):
        start = time.monotonic()
        queued_ms = round((start - first_seen.pop(record.get("id"), start)) * 1000)
        outcome = "exception"
        try:
            result = original(harness, record, now)
            outcome = "host_approved"
            return result
        finally:
            print(json.dumps({"qualification": "synthetic-admission-latency", "sample": sample,
                              "queue_ms": queued_ms, "duration_ms": round((time.monotonic() - start) * 1000),
                              "host_result": outcome}))

    monkeypatch.setattr(_Wire, "read", measured_read)
    monkeypatch.setattr(PiCodingHarness, "_confirm", measured)


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


def _synthetic_child_script(detached):
    child = "process.stdout.write('child-ready\\n'); setInterval(()=>{},60000)"
    return ("const {spawn}=require('child_process'); const child=spawn(process.execPath,['-e'," +
            json.dumps(child) + "],{detached:" + json.dumps(detached) + ",stdio:'inherit'}); " +
            "child.unref(); setTimeout(()=>{process.kill(child.pid,0); console.log('child-verified')},500); " +
            "setInterval(()=>{},60000)")


@contextmanager
def _paused_synthetic_guest(worker, *, detached=None):
    name = "lh-paused-diagnostic-" + uuid4().hex
    container = None
    try:
        container = worker._control("create", "--name=" + name, "--runtime=long-haul-gvisor", "--network=none",
            "--read-only", "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--entrypoint=/usr/local/bin/node", "--pull=never", worker.environment.image,
            "-e", "setInterval(()=>{},60000)" if detached is None else _synthetic_child_script(detached)).strip()
        worker._control("start", container)
        if detached is not None:
            deadline = time.monotonic() + 20
            while "child-verified" not in worker._control("logs", container):
                if time.monotonic() >= deadline:
                    raise AssertionError("synthetic child did not start")
                time.sleep(.1)
            assert "child-ready" in worker._control("logs", container)
        worker._control("pause", container)
        yield container, name
    finally:
        if container is not None:
            try:
                worker._control("unpause", container)
            except subprocess.SubprocessError:
                pass  # Only this synthetic guest may be resumed for cleanup.
            worker._control("rm", "--force", container)


def test_synthetic_gvisor_paused_kill_diagnostic(tmp_path):
    """Record provider behavior only; this does not qualify a stop boundary."""
    worker, _, _ = worker_setup(tmp_path, PLAN)
    with _paused_synthetic_guest(worker) as (container, name):
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


def _synthetic_runtime_state(worker, container):
    for root in ("/run/docker/runtime-long-haul-gvisor/moby", "/run/docker/runtime-runc/moby"):
        exists = subprocess.run(["sudo", "-n", "test", "-d", root], env=_control_environment(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        if exists.returncode != 0:
            continue
        result = subprocess.run(["sudo", "-n", str(worker.runsc_path), "--root", root, "state", container],
                                env=_control_environment(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=15, check=False)
        if result.returncode != 0:
            continue
        state = json.loads(result.stdout)
        assert state.get("id") == container
        assert state.get("status") in {"created", "running", "paused", "stopped"}
        return root, state
    return None, None


def test_hosted_synthetic_paused_runtime_state_diagnostic(tmp_path):
    """Observe Docker and runsc states without claiming a containment boundary."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        pytest.skip("synthetic state inspection requires an ephemeral GitHub-hosted runner")
    worker, _, _ = worker_setup(tmp_path, PLAN)
    with _paused_synthetic_guest(worker) as (container, _):
        info, = json.loads(worker._control("inspect", container))
        assert info["Id"] == container and info["State"].get("Paused") is True
        _, state = _synthetic_runtime_state(worker, container)
        runtime_state = "unavailable" if state is None else state["status"]
        print(json.dumps({"qualification": "synthetic-paused-runtime-state", "docker_paused": True,
                          "runtime_state": runtime_state}))


def _synthetic_container_cgroup(pid, container):
    membership = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
    unified = [line.removeprefix("0::") for line in membership if line.startswith("0::")]
    assert len(unified) == 1
    root = Path("/sys/fs/cgroup").resolve()
    group = (root / unified[0].lstrip("/")).resolve()
    assert group.is_relative_to(root) and group.name in {container, "docker-" + container + ".scope"}
    assert dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())["populated"] == "1"
    return group


@pytest.mark.parametrize("detached", [False, True])
def test_hosted_synthetic_paused_runtime_delete_diagnostic(tmp_path, detached):
    """Destroy only an exact disposable guest; this is not a production stop path."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        pytest.skip("synthetic destructive diagnostic requires an ephemeral GitHub-hosted runner")
    worker, _, _ = worker_setup(tmp_path, PLAN)
    with _paused_synthetic_guest(worker, detached=detached) as (container, _):
        assert len(container) == 64 and all(char in "0123456789abcdef" for char in container)
        info, = json.loads(worker._control("inspect", container))
        assert info["Id"] == container and info["State"].get("Paused") is True
        pid = info["State"].get("Pid")
        assert type(pid) is int and pid > 0
        selected, state = _synthetic_runtime_state(worker, container)
        if selected is None or state.get("pid") != pid or state.get("status") != "paused":
            pytest.fail("exact paused synthetic runtime identity could not be verified")
        group = _synthetic_container_cgroup(pid, container)
        result = subprocess.run(["sudo", "-n", str(worker.runsc_path), "--root", selected,
                                 "delete", "--force", container], env=_control_environment(),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45, check=False)
        try:
            events = dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())
            populated = events.get("populated", "unknown")
        except FileNotFoundError:
            populated = "removed"
        try:
            after, = json.loads(worker._control("inspect", container))
            docker_running = after["State"].get("Running")
            docker_paused = after["State"].get("Paused")
        except (OSError, subprocess.SubprocessError):
            docker_running = docker_paused = None
        print(json.dumps({"qualification": "synthetic-paused-runtime-delete", "returncode": result.returncode,
                          "cgroup_populated": populated, "docker_running": docker_running,
                          "docker_paused": docker_paused, "detached": detached, "child_started": True,
                          "production_stop_qualified": False}))
        assert result.returncode == 0 and populated in {"0", "removed"}
        assert docker_running is False and docker_paused is False


def _wait_synthetic_cgroup_empty(events):
    deadline = time.monotonic() + 10
    while True:
        try:
            populated = dict(line.split() for line in events.read_text().splitlines())["populated"]
        except FileNotFoundError:
            populated = "removed"
        if populated in {"0", "removed"}:
            return populated
        if time.monotonic() >= deadline:
            raise AssertionError("synthetic guest cgroup remained populated")
        time.sleep(.1)


@pytest.mark.parametrize("detached", [False, True])
def test_hosted_synthetic_unpaused_stop_empties_guest_cgroup(tmp_path, detached):
    """Observe a disposable guest with a live child; this is not production admission."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        pytest.skip("cgroup experiment requires an ephemeral GitHub-hosted runner")
    worker, _, _ = worker_setup(tmp_path, PLAN)
    name = "lh-stop-tree-" + uuid4().hex
    container = worker._control("create", "--name=" + name, "--runtime=long-haul-gvisor", "--network=none",
        "--read-only", "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--pids-limit=64", "--memory=256m", "--entrypoint=/usr/local/bin/node", "--pull=never",
        worker.environment.image, "-e", _synthetic_child_script(detached)).strip()
    try:
        worker._control("start", container)
        deadline = time.monotonic() + 20
        while "child-verified" not in worker._control("logs", container):
            if time.monotonic() >= deadline:
                raise AssertionError("synthetic child did not start")
            time.sleep(.1)
        assert "child-ready" in worker._control("logs", container)
        info, = json.loads(worker._control("inspect", container))
        state = info["State"]
        assert info["Id"] == container and state["Running"] is True and state["Paused"] is False
        pid = state["Pid"]
        assert isinstance(pid, int) and pid > 1
        relative = "/system.slice/docker-" + container + ".scope"
        assert "0::" + relative in Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        events = Path("/sys/fs/cgroup") / relative.lstrip("/") / "cgroup.events"
        assert dict(line.split() for line in events.read_text().splitlines())["populated"] == "1"
        worker._control("kill", "--signal=KILL", container)
        stopped, = json.loads(worker._control("inspect", container))
        assert stopped["Id"] == container and stopped["State"]["Running"] is False
        assert stopped["State"]["Paused"] is False and stopped["State"]["Pid"] == 0
        populated = _wait_synthetic_cgroup_empty(events)
        print(json.dumps({"qualification": "hosted-synthetic-unpaused-stop-tree", "detached": detached,
                          "child_started": True, "cgroup_observation": populated}))
    finally:
        worker._control("rm", "--force", container)


def test_synthetic_paused_cgroup_identity_diagnostic(tmp_path):
    """Inspect only the disposable guest's cgroup; do not alter it."""
    worker, _, _ = worker_setup(tmp_path, PLAN)
    with _paused_synthetic_guest(worker) as (container, _):
        info, = json.loads(worker._control("inspect", container))
        pid = info["State"]["Pid"]
        assert info["Id"] == container and info["State"]["Paused"] is True and isinstance(pid, int) and pid > 0
        rows = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        unified = [row.removeprefix("0::") for row in rows if row.startswith("0::")]
        assert len(unified) == 1
        root = Path("/sys/fs/cgroup").resolve()
        cgroup = (root / unified[0].lstrip("/")).resolve()
        assert cgroup.is_relative_to(root) and cgroup != root
        events = dict(line.split() for line in (cgroup / "cgroup.events").read_text().splitlines())
        members = (cgroup / "cgroup.procs").read_text().splitlines()
        commands = []
        for member in members:
            try:
                commands.append(Path(f"/proc/{int(member)}/cmdline").read_bytes().lower())
            except OSError:
                commands.append(b"")  # An unreadable or exited process makes this inconclusive.
        print(json.dumps({"qualification": "synthetic-paused-cgroup-inspection",
                          "container_leaf": cgroup.name in {container, "docker-" + container + ".scope"},
                          "frozen": events.get("frozen"), "populated": events.get("populated"),
                          "inspected_pid_is_member": str(pid) in members, "member_count": len(members),
                          "member_commands_readable": bool(members) and all(bool(command) for command in commands),
                          "runsc_seen": any(b"runsc" in command for command in commands),
                          "gofer_seen": any(b"gofer" in command for command in commands),
                          "kill_interface_present": (cgroup / "cgroup.kill").is_file()}))


_HOSTED_CGROUP_KILL = r"""
import json, os, re, signal, sys, time
pid, cid = int(sys.argv[1]), sys.argv[2]
mode = sys.argv[3] if len(sys.argv) == 4 else 'immediate'
if len(sys.argv) not in (3, 4) or pid <= 1 or not re.fullmatch('[0-9a-f]{64}', cid) or mode not in ('immediate', 'capture'):
    raise SystemExit('invalid synthetic guest identity')
relative = '/system.slice/docker-' + cid + '.scope'
membership = open('/proc/' + str(pid) + '/cgroup').read().splitlines()
if '0::' + relative not in membership:
    raise SystemExit('guest is not in its exact Docker systemd scope')
path = '/sys/fs/cgroup' + relative
directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
frozen = False
killed = False
try:
    if os.fstat(directory).st_ino != os.stat(path, follow_symlinks=False).st_ino:
        raise SystemExit('cgroup identity changed')
    procfile = os.open('cgroup.procs', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    with os.fdopen(procfile) as stream:
        if str(pid) not in stream.read().splitlines():
            raise SystemExit('guest pid is not a direct cgroup member')
    freezefile = os.open('cgroup.freeze', os.O_WRONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        os.write(freezefile, b'1')
        frozen = True
    finally:
        os.close(freezefile)
    deadline = time.monotonic() + 10
    while True:
        events = os.open('cgroup.events', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(events) as stream:
            values = dict(line.split() for line in stream.read().splitlines())
        if values.get('frozen') == '1' and values.get('populated') == '1':
            break
        if time.monotonic() >= deadline:
            raise SystemExit('synthetic cgroup did not freeze')
        time.sleep(.1)
    if mode == 'capture':
        def expired(signum, frame):
            raise TimeoutError('synthetic capture handshake expired')
        signal.signal(signal.SIGALRM, expired)
        signal.alarm(50)
        try:
            print(json.dumps({'kernel_freeze_confirmed': True}), flush=True)
            if sys.stdin.readline().strip() != 'kill':
                raise SystemExit('synthetic capture was not completed')
        finally:
            signal.alarm(0)
    killfile = os.open('cgroup.kill', os.O_WRONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        os.write(killfile, b'1')
        killed = True
    finally:
        os.close(killfile)
    deadline = time.monotonic() + 10
    while True:
        try:
            events = os.open('cgroup.events', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            with os.fdopen(events) as stream:
                values = dict(line.split() for line in stream.read().splitlines())
        except FileNotFoundError:
            observation = 'removed'
            break
        if values.get('populated') == '0':
            observation = 'empty'
            break
        if time.monotonic() >= deadline:
            raise SystemExit('synthetic cgroup remained populated')
        time.sleep(.1)
finally:
    if frozen and not killed:
        freezefile = os.open('cgroup.freeze', os.O_WRONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            os.write(freezefile, b'0')
        finally:
            os.close(freezefile)
    os.close(directory)
print(json.dumps({'kernel_freeze_confirmed': True, 'cgroup_kill_acknowledged': True,
                  'cgroup_observation': observation}))
"""


def _begin_synthetic_freeze(worker, guest):
    worker._control("pause", guest)
    info, = json.loads(worker._control("inspect", guest))
    assert info["Id"] == guest and info["State"]["Paused"] is True
    return subprocess.Popen(["sudo", "-n", "python3", "-c", _HOSTED_CGROUP_KILL,
                             str(info["State"]["Pid"]), guest, "capture"],
                            env=_control_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)


def _hosted_frozen_holder_capture(worker, guest, holder):
    """Observe a synthetic writer before and during a bounded kernel freeze."""
    _wait_synthetic_clock_advance(worker, holder)
    process = _begin_synthetic_freeze(worker, guest)
    try:
        assert select.select([process.stdout], [], [], 20)[0]
        assert json.loads(process.stdout.readline()).get("kernel_freeze_confirmed") is True
        frozen = copy_docker_workspace(worker.docker_path, holder, 15)
        time.sleep(.3)
        assert copy_docker_workspace(worker.docker_path, holder, 15) == frozen
        output, _ = process.communicate("kill\n", timeout=20)
        assert process.returncode == 0 and json.loads(output).get("cgroup_observation") in {"empty", "removed"}
        stopped, = json.loads(worker._control("inspect", guest))
        state = stopped["State"]
        assert (stopped["Id"] == guest and state["Running"] is False and state["Paused"] is False
                and state["Pid"] == 0 and state["ExitCode"] == 137)
        assert copy_docker_workspace(worker.docker_path, holder, 15) == frozen
        print(json.dumps({"qualification": "synthetic-frozen-holder-capture", "stable": True,
                          "stopped_and_reconciled": True}))
        return frozen
    finally:
        if process.poll() is None:
            process.stdin.close()  # EOF makes the synthetic helper thaw if it has not killed.
            process.wait(timeout=55)


def _synthetic_kernel_capture(worker, view, guest, holder, holder_profile):
    """Hosted-only experiment, not a privileged production capture authority."""
    process = _begin_synthetic_freeze(worker, guest)
    try:
        assert select.select([process.stdout], [], [], 20)[0]
        assert json.loads(process.stdout.readline()).get("kernel_freeze_confirmed") is True
        view._check(paused=True)
        files = copy_docker_workspace(worker.docker_path, holder, 15)
        view._check(paused=True)
        output, _ = process.communicate("kill\n", timeout=20)
        assert process.returncode == 0 and json.loads(output).get("cgroup_observation") in {"empty", "removed"}
        stopped, = json.loads(worker._control("inspect", guest))
        state = stopped["State"]
        assert (stopped["Id"] == guest and state["Running"] is False and state["Paused"] is False
                and state["Pid"] == 0 and state["Status"] == "exited" and state["ExitCode"] == 137)
        verify_workspace_holder(worker._control, worker._quota_intent, holder_profile, holder, running=True)
        assert copy_docker_workspace(worker.docker_path, holder, 15) == files
        worker._remove_exact(guest)
        return files, True
    finally:
        if process.poll() is None:
            process.stdin.close()
            process.wait(timeout=55)


def test_hosted_actual_pi_synthetic_kernel_capture(tmp_path, monkeypatch):
    """Test-only privileged path; no production admission or isolation claim."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        pytest.skip("synthetic privileged experiment requires an ephemeral GitHub-hosted runner")
    _record_synthetic_admission_latency(monkeypatch, "kernel-capture")
    with quota_fixture(tmp_path, PLAN) as (worker, request, profile, grant, root, intent):
        monkeypatch.setattr(worker, "_capture_and_remove",
                            lambda view, guest, holder, hp: _synthetic_kernel_capture(worker, view, guest, holder, hp))
        result, events, snapshot = worker.run_bounded(request, grant)
        assert result.terminal == "settled" and snapshot is not None and result.cleanup.process_tree_stopped
        assert all(event.outcome.value == "success" for event in events if event.phase == "end")
        assert b"a + b" in snapshot.contents["program.py"] and not worker.lease_releasable
        sandbox = DockerGVisorSandbox(environment(), Path(os.environ["LONG_HAUL_RUNSC"]))
        packet, evaluation = RepositoryEvaluationRunner(sandbox, scratch_root=grant.scratch_root()).evaluate(
            request.normalizer.contract, snapshot, profile, attempt_id="kernel-fixture", packet_id="kernel-fixture")
        assert packet.validity == "valid" and evaluation.disposition.value == "ACCEPTED"
    assert worker.lease_releasable and not (root / (intent.token + ".scratch")).exists()
    assert (root / (intent.token + ".cleanup.json")).is_file()
    print(json.dumps({"qualification": "hosted-actual-pi-synthetic-kernel-capture", "real_model": False}))


def _synthetic_runtime_delete_capture(worker, view, guest, holder, holder_profile):
    """Test-only privileged capture; Docker and host authority are not qualified."""
    worker._ensure_deadline()
    worker._control("pause", guest)
    view._check(paused=True)
    files = copy_docker_workspace(worker.docker_path, holder, 15)
    view._check(paused=True)
    info, = json.loads(worker._control("inspect", guest))
    pid = info["State"].get("Pid")
    assert info["Id"] == guest and info["State"].get("Paused") is True and type(pid) is int and pid > 0
    selected, state = _synthetic_runtime_state(worker, guest)
    assert selected is not None and state.get("pid") == pid and state.get("status") == "paused"
    group = _synthetic_container_cgroup(pid, guest)
    subprocess.run(["sudo", "-n", str(worker.runsc_path), "--root", selected, "delete", "--force", guest],
                   env=_control_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=45, check=True)
    population = _wait_synthetic_cgroup_empty(group / "cgroup.events")
    stopped, = json.loads(worker._control("inspect", guest))
    state = stopped["State"]
    assert stopped["Id"] == guest and state.get("Running") is False and state.get("Paused") is False
    assert state.get("Pid") == 0 and state.get("Status") == "exited" and state.get("ExitCode") == 137
    verify_workspace_holder(worker._control, worker._quota_intent, holder_profile, holder, running=True)
    view._check_volume_users()
    assert copy_docker_workspace(worker.docker_path, holder, 15) == files
    view._check_volume_users()
    worker._remove_exact(guest)
    verify_workspace_holder(worker._control, worker._quota_intent, holder_profile, holder, running=True)
    print(json.dumps({"qualification": "synthetic-runtime-delete-capture", "cgroup_observation": population,
                      "production_stop_qualified": False}))
    return files, True


def test_hosted_actual_pi_synthetic_runtime_delete_capture(tmp_path, monkeypatch):
    """Exercise a synthetic workspace only; no production authority is implied."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        pytest.skip("synthetic runtime deletion requires an ephemeral GitHub-hosted runner")
    with quota_fixture(tmp_path, PLAN) as (worker, request, profile, grant, root, intent):
        monkeypatch.setattr(worker, "_capture_and_remove",
                            lambda view, guest, holder, hp: _synthetic_runtime_delete_capture(worker, view, guest, holder, hp))
        result, events, snapshot = worker.run_bounded(request, grant)
        assert result.terminal == "settled" and snapshot is not None and result.cleanup.process_tree_stopped
        assert [event.outcome.value for event in events if event.phase == "end"] == ["success", "success"]
        assert b"a + b" in snapshot.contents["program.py"] and not worker.lease_releasable
        sandbox = DockerGVisorSandbox(environment(), Path(os.environ["LONG_HAUL_RUNSC"]))
        packet, evaluation = RepositoryEvaluationRunner(sandbox, scratch_root=grant.scratch_root()).evaluate(
            request.normalizer.contract, snapshot, profile, attempt_id="runtime-delete-fixture", packet_id="runtime-delete-fixture")
        assert packet.validity == "valid" and evaluation.disposition.value == "ACCEPTED"
    assert worker.lease_releasable and not (root / (intent.token + ".scratch")).exists()
    assert (root / (intent.token + ".cleanup.json")).is_file()


def test_hosted_actual_pi_synthetic_unpaused_stop(tmp_path, monkeypatch):
    """Explore stop-first capture without asserting a qualified process-tree boundary."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        pytest.skip("synthetic experiment requires an ephemeral GitHub-hosted runner")
    _record_synthetic_admission_latency(monkeypatch, "unpaused-stop")
    with quota_fixture(tmp_path, PLAN) as (worker, request, profile, grant, root, intent):
        def capture(view, guest, holder, holder_profile):
            view._check(paused=False)
            worker._control("kill", "--signal=KILL", guest)
            stopped, = json.loads(worker._control("inspect", guest))
            state = stopped["State"]
            assert stopped["Id"] == guest and state["Running"] is False and state["Paused"] is False
            assert state["Pid"] == 0 and state["Status"] == "exited" and state["ExitCode"] == 137
            assert state["OOMKilled"] is False and state["Dead"] is False and not state["Error"]
            verify_workspace_holder(worker._control, intent, holder_profile, holder, running=True)
            files = copy_docker_workspace(worker.docker_path, holder, 15)
            verify_workspace_holder(worker._control, intent, holder_profile, holder, running=True)
            assert copy_docker_workspace(worker.docker_path, holder, 15) == files
            worker._remove_exact(guest)
            verify_workspace_holder(worker._control, intent, holder_profile, holder, running=True)
            return files, True

        monkeypatch.setattr(worker, "_capture_and_remove", capture)
        result, events, snapshot = worker.run_bounded(request, grant)
        assert result.terminal == "settled" and snapshot is not None
        assert [event.outcome.value for event in events if event.phase == "end"] == ["success", "success"]
        assert b"a + b" in snapshot.contents["program.py"] and not worker.lease_releasable
        sandbox = DockerGVisorSandbox(environment(), Path(os.environ["LONG_HAUL_RUNSC"]))
        packet, evaluation = RepositoryEvaluationRunner(sandbox, scratch_root=grant.scratch_root()).evaluate(
            request.normalizer.contract, snapshot, profile, attempt_id="unpaused-fixture", packet_id="unpaused-fixture")
        assert packet.validity == "valid" and evaluation.disposition.value == "ACCEPTED"
    assert worker.lease_releasable and not (root / (intent.token + ".scratch")).exists()
    assert (root / (intent.token + ".cleanup.json")).is_file()
    print(json.dumps({"qualification": "hosted-actual-pi-synthetic-unpaused-stop", "real_model": False,
                      "process_tree_qualified": False}))


def _synthetic_long_confirm_timeout(monkeypatch):
    original = pi_quota_worker._prepare_control

    def prepare(root, attempt, tools, synthetic):
        if synthetic != PLAN:
            raise ValueError("timeout experiment requires the public synthetic plan")
        original(root, attempt, tools, synthetic)
        guard = root / "guard.mjs"
        source = guard.read_text(encoding="utf-8")
        if source.count("timeout: 15000") != 1:
            raise ValueError("unexpected synthetic guard source")
        root.chmod(0o755)
        guard.chmod(0o644)
        guard.write_text(source.replace("timeout: 15000", "timeout: 30000"), encoding="utf-8")
        guard.chmod(0o444)
        root.chmod(0o555)

    monkeypatch.setattr(pi_quota_worker, "_prepare_control", prepare)


def test_hosted_synthetic_long_confirm_timeout(tmp_path, monkeypatch):
    """Unqualified test-only guard variant; its identity differs from the worker receipt."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        pytest.skip("synthetic privileged experiment requires an ephemeral GitHub-hosted runner")
    _record_synthetic_admission_latency(monkeypatch, "synthetic-timeout-30s")
    _synthetic_long_confirm_timeout(monkeypatch)
    with quota_fixture(tmp_path, PLAN) as (worker, request, _, grant, _, _):
        monkeypatch.setattr(worker, "_capture_and_remove",
                            lambda view, guest, holder, hp: _synthetic_kernel_capture(worker, view, guest, holder, hp))
        result, events, snapshot = worker.run_bounded(request, grant)
        outcomes = [event.outcome.value for event in events if event.phase == "end"]
        print(json.dumps({"qualification": "synthetic-guard-timeout-comparison", "timeout_ms": 30000,
                          "control_identity_matches_receipt": False, "outcomes": outcomes}))
        assert result.terminal == "settled" and snapshot is not None and result.cleanup.process_tree_stopped
        assert outcomes == ["success", "success"] and b"a + b" in snapshot.contents["program.py"]


def test_hosted_actual_guard_tolerates_bounded_slow_confirmation(tmp_path, monkeypatch):
    """Use the receipt-bound guard, with a deliberate delay beyond the old limit."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        pytest.skip("synthetic experiment requires an ephemeral GitHub-hosted runner")
    original = PiCodingHarness._confirm
    delays = []

    def delayed(harness, record, now):
        if not delays:
            time.sleep(6)
            delays.append(6)
        return original(harness, record, now)

    monkeypatch.setattr(PiCodingHarness, "_confirm", delayed)
    with quota_fixture(tmp_path, PLAN) as (worker, request, _, grant, _, _):
        monkeypatch.setattr(worker, "_capture_and_remove",
                            lambda view, guest, holder, hp: _synthetic_kernel_capture(worker, view, guest, holder, hp))
        result, events, snapshot = worker.run_bounded(request, grant)
        assert delays == [6] and result.terminal == "settled" and snapshot is not None
        assert [event.outcome.value for event in events if event.phase == "end"] == ["success", "success"]
        assert b"a + b" in snapshot.contents["program.py"]
    print(json.dumps({"qualification": "hosted-bound-guard-slow-confirmation", "delay_seconds": 6,
                      "synthetic_provider": True, "privileged_test_capture": True}))


def _assert_synthetic_capture_abort_thaws(pid, container):
    process = subprocess.Popen(["sudo", "-n", "python3", "-c", _HOSTED_CGROUP_KILL,
                                str(pid), container, "capture"], env=_control_environment(),
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        assert select.select([process.stdout], [], [], 20)[0]
        assert json.loads(process.stdout.readline()).get("kernel_freeze_confirmed") is True
    finally:
        process.stdin.close()
        process.wait(timeout=55)
    assert process.returncode != 0
    events_file = Path("/sys/fs/cgroup/system.slice") / ("docker-" + container + ".scope/cgroup.events")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        events = dict(line.split() for line in events_file.read_text().splitlines())
        if events.get("frozen") == "0" and events.get("populated") == "1":
            return
        time.sleep(.1)
    raise AssertionError("synthetic cgroup did not thaw after aborted capture")


def test_hosted_synthetic_cgroup_kill_experiment(tmp_path):
    """Experiment only on a disposable synthetic guest in hosted CI."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        pytest.skip("synthetic privileged experiment requires an ephemeral GitHub-hosted runner")
    worker, _, _ = worker_setup(tmp_path, PLAN)
    with _paused_synthetic_guest(worker) as (container, _):
        info, = json.loads(worker._control("inspect", container))
        assert info["Id"] == container and info["State"]["Paused"] is True
        _assert_synthetic_capture_abort_thaws(info["State"]["Pid"], container)
        other_id = ("0" if container[0] != "0" else "1") + container[1:]
        rejected = subprocess.run(["sudo", "-n", "python3", "-c", _HOSTED_CGROUP_KILL,
                                   str(info["State"]["Pid"]), other_id],
                                  env=_control_environment(), capture_output=True, timeout=20, check=False)
        assert rejected.returncode != 0 and b'exact Docker systemd scope' in rejected.stderr
        unchanged, = json.loads(worker._control("inspect", container))
        assert unchanged["Id"] == container and unchanged["State"]["Running"] is True
        assert unchanged["State"]["Paused"] is True and unchanged["State"]["Pid"] == info["State"]["Pid"]
        print(json.dumps({"qualification": "synthetic-cgroup-negative-cases", "abort_thawed": True,
                          "identity_rejected_without_stop": True}))
        result = subprocess.run(["sudo", "-n", "python3", "-c", _HOSTED_CGROUP_KILL,
                                 str(info["State"]["Pid"]), container],
                                env=_control_environment(), capture_output=True, timeout=20, check=False)
        observed = json.loads(result.stdout) if result.returncode == 0 else {}
        after, = json.loads(worker._control("inspect", container))
        state = after["State"]
        print(json.dumps({"qualification": "synthetic-hosted-cgroup-kill", "exit_code": result.returncode,
                          "identity_mismatch": b'exact Docker systemd scope' in result.stderr,
                          "cgroup_observation": observed.get("cgroup_observation"),
                          "docker_running": state.get("Running"), "docker_paused": state.get("Paused"),
                          "docker_status": state.get("Status"), "docker_pid_zero": state.get("Pid") == 0,
                          "docker_exit_137": state.get("ExitCode") == 137}))
        assert result.returncode == 0 and observed.get("kernel_freeze_confirmed") is True


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
