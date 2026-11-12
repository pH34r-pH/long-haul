"""Quota executor failure boundaries; real Pi/gVisor is qualified separately."""
import json
import os
from types import SimpleNamespace

import pytest
from test_pi_workspace_cleanup import setup

from long_haul.adapters import pi_quota_worker
from long_haul.adapters.pi_quota_worker import QuotaPiWorker
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


@pytest.mark.parametrize("changed", [False, True])
def test_candidate_requires_matching_post_stop_snapshot(monkeypatch, changed):
    worker = object.__new__(QuotaPiWorker)
    worker._execution_deadline = float("inf")
    worker.docker_path, worker._quota_intent = "/fixture/docker", object()
    calls = []

    def control(*args):
        calls.append(args)
        if args[0] == "inspect":
            return json.dumps([{"Id": "a" * 64, "State": {"Running": False, "Paused": False, "Pid": 0, "Error": ""}}])
        return ""

    worker._control = control
    worker._remove_exact = lambda _: calls.append(("remove",))
    view = SimpleNamespace(_check=lambda **_: calls.append(("check",)))
    snapshots = iter([{"a": b"before"}, {"a": b"after" if changed else b"before"}])
    monkeypatch.setattr(pi_quota_worker, "copy_docker_workspace", lambda *_: next(snapshots))
    monkeypatch.setattr(pi_quota_worker, "verify_workspace_holder", lambda *args, **kwargs: None)
    if changed:
        with pytest.raises(ValueError, match="changed while stopping"):
            worker._capture_and_remove(view, "a" * 64, "b" * 64, object())
        assert ("remove",) not in calls
    else:
        assert worker._capture_and_remove(view, "a" * 64, "b" * 64, object()) == {"a": b"before"}
        assert calls.index(("kill", "--signal=KILL", "a" * 64)) < calls.index(("remove",))
