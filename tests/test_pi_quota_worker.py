"""Quota executor failure boundaries; real Pi/gVisor is qualified separately."""
import os

import pytest
from test_pi_workspace_cleanup import setup

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
