"""Quota executor failure boundaries; real Pi/gVisor is qualified separately."""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest
from test_pi_workspace_cleanup import setup

from long_haul.adapters import pi_quota_worker
from long_haul.adapters.pi_quota_worker import QuotaPiWorker
from long_haul.adapters.pi_worker import DockerGVisorPiWorker
from long_haul.adapters.pi_workspace_cleanup import WorkspaceCleanupRequired
from long_haul.adapters.pi_workspace_fence import LocalWorkspaceFence

pytestmark = pytest.mark.skipif(os.name != "posix", reason="workspace recovery requires POSIX filesystem semantics")


def test_quota_executor_never_falls_back_to_legacy_run():
    worker = object.__new__(QuotaPiWorker)
    with pytest.raises(TypeError, match="run_bounded"):
        worker.run(None)
    with pytest.raises(TypeError, match="active workspace fence"):
        worker.run_bounded(None, None)
    with pytest.raises(TypeError, match="recover_bounded"):
        worker.recover()
    worker._execution_deadline = 0
    with pytest.raises(TimeoutError, match="deadline"):
        worker._ensure_deadline()


def test_unclean_quota_attempt_never_releases_lease_until_matching_recovery(tmp_path):
    root, intent, docker = setup(tmp_path)
    fence = LocalWorkspaceFence(root, intent.token, intent.fingerprint)
    worker = object.__new__(QuotaPiWorker)
    worker._control = docker
    with pytest.raises(WorkspaceCleanupRequired), fence.launch() as grant:
        worker._quota_grant = grant
        assert not worker.lease_releasable
    assert grant.closed and not grant.cleaned and not worker.lease_releasable
    with pytest.raises(WorkspaceCleanupRequired):
        worker.recover_bounded(LocalWorkspaceFence(root, "9" * 32, intent.fingerprint))
    assert docker.calls == []
    assert worker.recover_bounded(fence).exists() and worker.lease_releasable


def test_quota_executor_rejects_direct_event_store_before_allocation(tmp_path):
    root, intent, docker = setup(tmp_path)
    worker = object.__new__(QuotaPiWorker)
    worker._control = docker
    worker.environment = SimpleNamespace(limits=SimpleNamespace(wall_seconds=10))
    worker._validate_request = lambda _: None
    request = SimpleNamespace(normalizer=SimpleNamespace(contract=SimpleNamespace(budget=SimpleNamespace(wall_seconds=10))),
                              event_store=object())
    with LocalWorkspaceFence(root, intent.token, intent.fingerprint).launch() as grant:
        with pytest.raises(ValueError, match="post-cleanup evidence"):
            worker.run_bounded(request, grant)
        assert docker.calls == [] and not worker.lease_releasable
        grant.cleanup(docker)


def stopped_worker_fixture():
    worker = object.__new__(QuotaPiWorker)
    worker._execution_deadline = float("inf")
    worker.docker_path, worker._quota_intent = "/fixture/docker", object()
    calls = []

    def control(*args):
        calls.append(args)
        if args[0] == "inspect":
            return json.dumps([{"Id": "a" * 64, "State": {"Running": False, "Paused": False, "Pid": 0,
                "Status": "exited", "ExitCode": 137, "OOMKilled": False, "Dead": False, "Error": ""}}])
        return ""
    worker._control = control
    worker._remove_exact = lambda _: calls.append(("remove",))
    return worker, calls


@pytest.mark.parametrize("changed", [False, True])
def test_candidate_requires_matching_post_stop_snapshot(monkeypatch, changed):
    worker, calls = stopped_worker_fixture()
    view = SimpleNamespace(_check=lambda **kw: calls.append(("check", kw["paused"])), _check_volume_users=lambda: None)
    snapshots = iter([{"a": b"before"}, {"a": b"after" if changed else b"before"}])
    def copy(*_):
        calls.append(("copy",))
        return next(snapshots)
    monkeypatch.setattr(pi_quota_worker, "copy_docker_workspace", copy)
    monkeypatch.setattr(pi_quota_worker, "verify_workspace_holder", lambda *args, **kwargs: None)
    if changed:
        with pytest.raises(ValueError, match="changed after stopping"):
            worker._capture_and_remove(view, "a" * 64, "b" * 64, object())
        assert ("remove",) not in calls
    else:
        assert worker._capture_and_remove(view, "a" * 64, "b" * 64, object()) == ({"a": b"before"}, True)
        assert ("pause", "a" * 64) not in calls
        assert calls.index(("check", False)) < calls.index(("kill", "--signal=KILL", "a" * 64))
        assert calls.index(("kill", "--signal=KILL", "a" * 64)) < calls.index(("copy",)) < calls.index(("remove",))


@pytest.mark.parametrize("stopped", [False, True])
def test_ambiguous_kill_is_never_retried_and_requires_exact_stopped_state(monkeypatch, stopped):
    worker = object.__new__(QuotaPiWorker)
    worker._execution_deadline = float("inf")
    worker.docker_path, worker._quota_intent = "/fixture/docker", object()
    calls = []

    def control(*args):
        calls.append(args)
        if args[0] == "kill":
            raise subprocess.CalledProcessError(1, args)
        if args[0] == "inspect":
            return json.dumps([{"Id": "a" * 64, "State": {"Running": not stopped, "Paused": not stopped,
                "Pid": 0 if stopped else 42, "Status": "exited" if stopped else "paused", "ExitCode": 137,
                "OOMKilled": False, "Dead": False, "Error": ""}}])
        return ""

    worker._control = control
    worker._remove_exact = lambda _: calls.append(("remove",))
    view = SimpleNamespace(_check=lambda **_: None, _check_volume_users=lambda: None)
    def copy(*_):
        calls.append(("copy",))
        return {"a": b"fixture"}
    monkeypatch.setattr(pi_quota_worker, "copy_docker_workspace", copy)
    monkeypatch.setattr(pi_quota_worker, "verify_workspace_holder", lambda *args, **kwargs: None)
    if stopped:
        assert worker._capture_and_remove(view, "a" * 64, "b" * 64, object()) == ({"a": b"fixture"}, False)
        assert ("remove",) in calls
    else:
        with pytest.raises(ValueError, match="stop is unconfirmed"):
            worker._capture_and_remove(view, "a" * 64, "b" * 64, object())
        assert ("remove",) not in calls and ("copy",) not in calls
    assert sum(call[0] == "kill" for call in calls) == 1


def test_final_capture_holds_if_volume_user_changes_after_stop(monkeypatch):
    worker, calls = stopped_worker_fixture()

    def volume_users():
        calls.append(("users",))
        if sum(call == ("users",) for call in calls) == 2:
            raise ValueError("workspace volume users changed")

    view = SimpleNamespace(_check=lambda **_: None, _check_volume_users=volume_users)
    monkeypatch.setattr(pi_quota_worker, "copy_docker_workspace", lambda *_: {"a": b"fixture"})
    monkeypatch.setattr(pi_quota_worker, "verify_workspace_holder", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="volume users changed"):
        worker._capture_and_remove(view, "a" * 64, "b" * 64, object())
    assert ("remove",) not in calls


@pytest.mark.parametrize("changed_after_copy", [1, 2])
def test_final_capture_rejects_worker_restart_during_snapshot(monkeypatch, changed_after_copy):
    worker, calls = stopped_worker_fixture()
    original_control = worker._control
    copies = [0]

    def control(*args):
        if args[0] == "inspect" and copies[0] >= changed_after_copy:
            return json.dumps([{"Id": "a" * 64, "State": {"Running": True, "Paused": False, "Pid": 42,
                "Status": "running", "ExitCode": 0, "OOMKilled": False, "Dead": False, "Error": ""}}])
        return original_control(*args)

    def copy(*_):
        copies[0] += 1
        return {"a": b"fixture"}

    worker._control = control
    view = SimpleNamespace(_check=lambda **_: None, _check_volume_users=lambda: None)
    monkeypatch.setattr(pi_quota_worker, "copy_docker_workspace", copy)
    monkeypatch.setattr(pi_quota_worker, "verify_workspace_holder", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="stop is unconfirmed"):
        worker._capture_and_remove(view, "a" * 64, "b" * 64, object())
    assert ("remove",) not in calls


@pytest.mark.parametrize("expires_at", ["stop", "post_stop_copy", "removal"])
def test_capture_does_not_return_candidate_after_deadline(monkeypatch, expires_at):
    worker, calls = stopped_worker_fixture()
    worker._execution_deadline = 10
    clock = [0]
    original_control = worker._control
    copies = [0]

    def control(*args):
        result = original_control(*args)
        if args[0] == "kill" and expires_at == "stop":
            clock[0] = 10
        return result

    def copy(*_):
        copies[0] += 1
        if copies[0] == 2 and expires_at == "post_stop_copy":
            clock[0] = 10
        return {"a": b"fixture"}

    worker._control = control
    if expires_at == "removal":
        def remove(_):
            calls.append(("remove",))
            clock[0] = 10

        worker._remove_exact = remove
    view = SimpleNamespace(_check=lambda **_: None, _check_volume_users=lambda: None)
    monkeypatch.setattr(pi_quota_worker.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(pi_quota_worker, "copy_docker_workspace", copy)
    monkeypatch.setattr(pi_quota_worker, "verify_workspace_holder", lambda *args, **kwargs: None)
    with pytest.raises(TimeoutError, match="deadline"):
        worker._capture_and_remove(view, "a" * 64, "b" * 64, object())
    assert (("remove",) in calls) is (expires_at == "removal")


@pytest.mark.parametrize("change", [
    {"LogConfig": {"Type": "json-file"}},
    {"RestartPolicy": {"Name": "always"}},
    {"AutoRemove": True},
])
def test_quota_worker_rejects_uncontrolled_logs_or_restart(monkeypatch, change):
    worker = object.__new__(QuotaPiWorker)
    worker._quota_intent = SimpleNamespace(worker_name="fixture-worker", labels=lambda _: {"fixture": "bound"})
    host = {"LogConfig": {"Type": "none"}, "RestartPolicy": {"Name": "no"}, "AutoRemove": False}
    info = {"Name": "/fixture-worker", "Config": {"Labels": {"fixture": "bound"}}, "HostConfig": host}
    monkeypatch.setattr(DockerGVisorPiWorker, "_verify_configuration", lambda *args: info)
    monkeypatch.setattr(pi_quota_worker, "_verify_privileges", lambda _: None)
    assert worker._verify_configuration("container", None) is info
    host.update(change)
    with pytest.raises(ValueError, match="identity"):
        worker._verify_configuration("container", None)
