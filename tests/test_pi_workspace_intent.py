"""Pre-allocation receipt tests; these do not grant cleanup authority."""
import os
from pathlib import Path

import pytest

from long_haul.adapters.pi_workspace_intent import (
    WorkspaceLeaseIntent,
    load_workspace_intent,
    persist_workspace_intent,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="durable private intent uses POSIX directory handles")


def intent():
    return WorkspaceLeaseIntent(token="a" * 32, attempt_fingerprint="sha256:" + "b" * 64,
                                environment_fingerprint="sha256:" + "c" * 64, daemon_id="fixture-daemon")


def private_root(tmp_path):
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    return root.resolve()


def test_intent_is_private_synced_and_exactly_bound_to_admitted_fingerprint(tmp_path, monkeypatch):
    root, receipt = private_root(tmp_path), intent()
    syncs, original = [], os.fsync

    def fsync(descriptor):
        syncs.append(descriptor)
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    path = persist_workspace_intent(root, receipt)
    assert len(syncs) == 2 and path.stat().st_mode & 0o777 == 0o600
    assert load_workspace_intent(root, receipt.token, receipt.fingerprint) == receipt
    assert receipt.worker_name == "lh-worker-" + receipt.token
    assert receipt.labels("volume")["dev.ph34r.long-haul.role"] == "volume"
    assert receipt.token not in repr(receipt)
    with pytest.raises(FileExistsError):
        persist_workspace_intent(root, receipt)
    with pytest.raises(ValueError, match="not admitted"):
        load_workspace_intent(root, receipt.token, "sha256:" + "d" * 64)


def test_intent_rejects_unprivate_or_symlinked_recovery_root(tmp_path):
    root = private_root(tmp_path)
    root.chmod(0o755)
    with pytest.raises(ValueError, match="private and stable"):
        persist_workspace_intent(root, intent())
    root.chmod(0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="non-symlink"):
        persist_workspace_intent(alias.absolute(), intent())


def test_intent_rejects_symlink_hardlink_and_modified_payload(tmp_path):
    root, receipt = private_root(tmp_path), intent()
    path = persist_workspace_intent(root, receipt)
    alias = root / "alias"
    os.link(path, alias)
    with pytest.raises(ValueError, match="not private"):
        load_workspace_intent(root, receipt.token, receipt.fingerprint)
    alias.unlink()
    path.write_text(path.read_text().replace("fixture-daemon", "other-daemon"))
    with pytest.raises(ValueError, match="not admitted"):
        load_workspace_intent(root, receipt.token, receipt.fingerprint)
    path.unlink()
    path.symlink_to(Path("/etc/passwd"))
    with pytest.raises(OSError):
        load_workspace_intent(root, receipt.token, receipt.fingerprint)


def test_failed_sync_never_returns_an_admitted_intent(tmp_path, monkeypatch):
    root = private_root(tmp_path)
    monkeypatch.setattr(os, "fsync", lambda _: (_ for _ in ()).throw(OSError("sync failed")))
    with pytest.raises(OSError, match="sync failed"):
        persist_workspace_intent(root, intent())
