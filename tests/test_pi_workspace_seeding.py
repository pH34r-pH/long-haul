"""Seed admission and readback tests; scheduler fencing remains external."""
import os
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from test_pi_workspace_cleanup import setup
from test_pi_workspace_holder import AllocationDocker, profile

from long_haul.adapters import pi_workspace_seeding
from long_haul.adapters.pi_workspace_cleanup import WorkspaceCleanupRequired
from long_haul.adapters.pi_workspace_fence import LocalWorkspaceFence
from long_haul.adapters.pi_workspace_seeding import HolderSeeder

pytestmark = pytest.mark.skipif(os.name != "posix", reason="recovery intents require POSIX filesystem semantics")


@contextmanager
def setup_seeder(tmp_path, monkeypatch, admit=lambda _: True):
    root, intent, _ = setup(tmp_path)
    control = AllocationDocker(intent)
    control.volume = intent.labels("volume")
    control.containers[intent.holder_name] = ("b" * 64, intent.labels("holder"))
    monkeypatch.setattr(pi_workspace_seeding, "verify_workspace_holder", lambda *args, **kwargs: None)
    with LocalWorkspaceFence(root, intent.token, intent.fingerprint).launch() as grant:
        seeder = HolderSeeder(root, intent.token, intent.fingerprint, control, Path("/usr/bin/docker"),
                              "b" * 64, profile(), admit, grant)
        try:
            yield seeder, intent, control
        finally:
            grant.cleanup(control)


def test_seed_checks_admission_empty_holder_and_exact_readback(tmp_path, monkeypatch):
    admitted, writes = [], []
    with setup_seeder(tmp_path, monkeypatch, lambda digest: admitted.append(digest) or True) as (seeder, _, _):
        snapshots = iter([{}, {"fixture/a": b"fixture"}])
        monkeypatch.setattr(pi_workspace_seeding, "copy_docker_workspace", lambda *_: next(snapshots))
        monkeypatch.setattr(pi_workspace_seeding.subprocess, "run", lambda *args, **kwargs: writes.append(kwargs["input"]))
        seeder.seed({"fixture/a", "fixture/new/b"}, {"fixture/a": b"fixture"})
        assert len(writes) == 1 and len(admitted) == 4 and len(set(admitted)) == 1
        with pytest.raises(WorkspaceCleanupRequired):
            seeder.seed({"fixture/a", "fixture/new/b"}, {"fixture/a": b"fixture"})
        assert len(writes) == 1


def test_seed_without_exact_held_fence_cannot_touch_docker(tmp_path, monkeypatch):
    with setup_seeder(tmp_path, monkeypatch) as (seeder, intent, control):
        unfenced = replace(seeder, grant=None)
        with pytest.raises(WorkspaceCleanupRequired):
            unfenced.seed({"fixture/a"}, {"fixture/a": b"fixture"})
        assert control.calls == []
        assert not (seeder.root / (intent.token + ".seed-started")).exists()


@pytest.mark.parametrize("case", ["denied", "worker-exists", "nonempty", "readback", "revoked-after-write"])
def test_seed_failure_never_retries_or_claims_success(tmp_path, monkeypatch, case):
    calls, writes = [], []

    def admit(digest):
        calls.append(digest)
        return case != "denied" and not (case == "revoked-after-write" and len(calls) == 3)

    with setup_seeder(tmp_path, monkeypatch, admit) as (seeder, intent, control):
        if case == "worker-exists":
            control.containers[intent.worker_name] = ("a" * 64, intent.labels("worker"))
        snapshots = iter([{"unexpected": b"x"}] if case == "nonempty" else [{}, {"fixture/a": b"wrong"}])
        monkeypatch.setattr(pi_workspace_seeding, "copy_docker_workspace", lambda *_: next(snapshots))
        monkeypatch.setattr(pi_workspace_seeding.subprocess, "run", lambda *args, **kwargs: writes.append(kwargs["input"]))
        with pytest.raises(WorkspaceCleanupRequired):
            seeder.seed({"fixture/a"}, {"fixture/a": b"fixture"})
        assert len(writes) == (1 if case in {"readback", "revoked-after-write"} else 0)
        assert not (seeder.root / (intent.token + ".cleanup.json")).exists()


def test_ambiguous_copy_failure_keeps_private_intent_and_never_retries(tmp_path, monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(None)
        raise subprocess.CalledProcessError(1, args[0], output=b"private provider data")

    with setup_seeder(tmp_path, monkeypatch) as (seeder, intent, control):
        monkeypatch.setattr(pi_workspace_seeding, "copy_docker_workspace", lambda *_: {})
        monkeypatch.setattr(pi_workspace_seeding.subprocess, "run", fail)
        with pytest.raises(WorkspaceCleanupRequired) as failure:
            seeder.seed({"fixture/a"}, {"fixture/a": b"fixture"})
        assert len(calls) == 1 and control.volume is not None and control.containers
        assert (seeder.root / (intent.token + ".json")).exists()
        assert "private provider data" not in str(failure.value)
