"""Local workspace fence tests; no distributed scheduler claim."""
import os
import select
import subprocess
import sys
import threading

import pytest
from test_pi_workspace_cleanup import FakeDocker, setup

from long_haul.adapters.pi_workspace_cleanup import WorkspaceCleanupRequired
from long_haul.adapters.pi_workspace_fence import LocalWorkspaceFence
from long_haul.adapters.pi_workspace_intent import (
    WorkspaceLeaseIntent,
    persist_workspace_intent,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="local fencing requires POSIX flock and directory handles")
DIGEST = "sha256:" + "f" * 64


def second_intent(root, first):
    other = WorkspaceLeaseIntent(token="9" * 32, attempt_fingerprint=first.attempt_fingerprint,
                                 environment_fingerprint=first.environment_fingerprint, daemon_id=first.daemon_id)
    persist_workspace_intent(root, other)
    return other


def test_fence_sequences_one_seed_worker_and_cleanup(tmp_path):
    root, intent, docker = setup(tmp_path)
    fence = LocalWorkspaceFence(root, intent.token, intent.fingerprint)
    with fence.launch() as grant:
        assert grant.admit(intent.fingerprint)
        with pytest.raises(ValueError, match="not ready"):
            grant.begin_worker()
        grant.begin_seed(DIGEST)
        assert grant.admit_seed(DIGEST) and not grant.admit_seed("sha256:" + "a" * 64)
        assert not grant.admit(intent.fingerprint)
        grant.finish_seed(DIGEST)
        assert not grant.admit_seed(DIGEST)
        grant.begin_worker()
        with pytest.raises(ValueError, match="single worker"):
            grant.begin_worker()
        assert grant.cleanup(docker).exists()
    assert not (root / "active.json").exists()
    with pytest.raises(ValueError, match="no longer held"):
        grant.admit(intent.fingerprint)
    with pytest.raises(FileExistsError), fence.launch():
        pass


def test_unclean_exit_blocks_new_token_until_exact_recovery(tmp_path):
    root, first, docker = setup(tmp_path)
    other = second_intent(root, first)
    fence = LocalWorkspaceFence(root, first.token, first.fingerprint)
    with pytest.raises(WorkspaceCleanupRequired), fence.launch():
        pass
    assert (root / "active.json").exists()
    with pytest.raises(WorkspaceCleanupRequired), LocalWorkspaceFence(root, other.token, other.fingerprint).launch():
        pass
    assert not (root / (other.token + ".used")).exists()
    assert fence.recover(docker).exists()
    other_docker = FakeDocker(other)
    with LocalWorkspaceFence(root, other.token, other.fingerprint).launch() as grant:
        assert grant.cleanup(other_docker).exists()


def test_grant_cannot_be_used_from_another_thread(tmp_path):
    root, intent, docker = setup(tmp_path)
    errors = []
    with LocalWorkspaceFence(root, intent.token, intent.fingerprint).launch() as grant:
        def attempt():
            try:
                grant.admit(intent.fingerprint)
            except ValueError as error:
                errors.append(str(error))

        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive() and errors == ["workspace fence is no longer held by its owner"]
        grant.cleanup(docker)


def test_tampered_active_marker_cannot_authorize_cleanup(tmp_path):
    root, intent, docker = setup(tmp_path)
    fence = LocalWorkspaceFence(root, intent.token, intent.fingerprint)
    with pytest.raises(WorkspaceCleanupRequired), fence.launch():
        pass
    (root / "active.json").write_text("foreign")
    with pytest.raises(WorkspaceCleanupRequired):
        fence.recover(docker)
    assert not any(call[0] == "rm" or call[:2] == ("volume", "rm") for call in docker.calls)


def test_crashed_controller_leaves_fence_until_recovery(tmp_path):
    root, intent, docker = setup(tmp_path)
    other = second_intent(root, intent)
    script = ("import sys,time; from pathlib import Path; "
              "from long_haul.adapters.pi_workspace_fence import LocalWorkspaceFence; "
              "f=LocalWorkspaceFence(Path(sys.argv[1]),sys.argv[2],sys.argv[3]); "
              "ctx=f.launch(); ctx.__enter__(); print('held',flush=True); time.sleep(30)")
    process = subprocess.Popen([sys.executable, "-c", script, str(root), intent.token, intent.fingerprint],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline().strip() == "held"
        with pytest.raises(BlockingIOError), LocalWorkspaceFence(root, other.token, other.fingerprint).launch():
            pass
    finally:
        process.kill()
        process.communicate(timeout=5)
    with pytest.raises(WorkspaceCleanupRequired), LocalWorkspaceFence(root, other.token, other.fingerprint).launch():
        pass
    assert LocalWorkspaceFence(root, intent.token, intent.fingerprint).recover(docker).exists()
