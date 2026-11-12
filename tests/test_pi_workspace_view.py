"""Host-owned workspace view tests; no claim of full worker isolation."""
import json
import os
from pathlib import Path

import pytest

from long_haul.adapters import pi_workspace_view
from long_haul.adapters.pi_workspace_view import PausedWorkspaceView

WORKER, HOLDER = "a" * 64, "b" * 64
VOLUME = "lh-workspace-" + "c" * 32
OPTIONS = "size=8m,nr_inodes=1024,mode=0777,nosuid,nodev,noexec"


class FakeControl:
    def __init__(self):
        self.paused = False
        self.calls = []
        self.options = OPTIONS

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] in {"pause", "unpause"}:
            self.paused = args[0] == "pause"
            return ""
        if args[:2] == ("volume", "inspect"):
            return json.dumps([{"Name": VOLUME, "Driver": "local", "Scope": "local", "Mountpoint": "/private/volume",
                                "Options": {"type": "tmpfs", "device": "tmpfs", "o": self.options}}])
        if args[0] == "inspect":
            mount = {"Destination": "/workspace", "Type": "volume", "Name": VOLUME,
                     "Source": "/private/volume", "RW": True}
            return json.dumps([{"Id": WORKER, "State": {"Running": True, "Paused": self.paused}, "Mounts": [mount]},
                               {"Id": HOLDER, "State": {"Running": True, "Paused": False}, "Mounts": [mount]}])
        raise AssertionError(args)


def make_view(control, verify=lambda: None):
    return PausedWorkspaceView(control, Path("/usr/bin/docker"), WORKER, HOLDER, VOLUME, OPTIONS, verify)


@pytest.mark.skipif(os.name != "posix", reason="private view modes require POSIX filesystem semantics")
def test_view_pauses_copies_and_holds_until_inspection_finishes(monkeypatch):
    control, checks = FakeControl(), []
    monkeypatch.setattr(pi_workspace_view, "copy_docker_workspace", lambda *_: {"dir/file": b"fixture"})
    with make_view(control, lambda: checks.append(control.paused))() as root:
        assert control.paused and (root / "dir/file").read_bytes() == b"fixture"
        assert root.stat().st_mode & 0o077 == 0
        assert (root / "dir/file").stat().st_mode & 0o077 == 0
    assert not root.exists() and not control.paused
    assert checks == [False, True, True, True, False]
    assert ("pause", WORKER) in control.calls and ("unpause", WORKER) in control.calls


def test_failed_inspection_does_not_resume_untrusted_worker(monkeypatch):
    control = FakeControl()
    monkeypatch.setattr(pi_workspace_view, "copy_docker_workspace", lambda *_: {"dir/file": b"fixture"})
    with pytest.raises(ValueError, match="denied"), make_view(control)():
        raise ValueError("denied")
    assert control.paused and ("unpause", WORKER) not in control.calls


def test_changed_volume_options_after_pause_leaves_worker_paused(monkeypatch):
    control = FakeControl()

    def copy(*_):
        control.options = "size=1g"
        return {}

    monkeypatch.setattr(pi_workspace_view, "copy_docker_workspace", copy)
    with pytest.raises(ValueError, match="binding changed"), make_view(control)():
        pass
    assert control.paused and ("unpause", WORKER) not in control.calls


@pytest.mark.parametrize("files", [{"../escape": b"x"}, {"a": b"x" * (1048576 + 1)}])
def test_invalid_snapshot_is_not_materialized_or_resumed(monkeypatch, files):
    control = FakeControl()
    monkeypatch.setattr(pi_workspace_view, "copy_docker_workspace", lambda *_: files)
    with pytest.raises(ValueError), make_view(control)():
        pass
    assert control.paused and ("unpause", WORKER) not in control.calls


@pytest.mark.parametrize("options", ["size=1g", OPTIONS.replace("1024", "1025"), OPTIONS.replace(",noexec", "")])
def test_view_requires_explicit_bounded_volume_options(options):
    with pytest.raises(ValueError, match="invalid paused workspace binding"):
        PausedWorkspaceView(FakeControl(), Path("/usr/bin/docker"), WORKER, HOLDER, VOLUME, options, lambda: None)
