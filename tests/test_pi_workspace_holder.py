"""Allocation sequencing tests; holder qualification is separately hosted."""
import os

import pytest
from test_pi_workspace_cleanup import FakeDocker, setup

from long_haul.adapters import pi_workspace_holder
from long_haul.adapters.pi_workspace_cleanup import (
    WorkspaceCleanupRequired,
    recover_workspace_lease,
)
from long_haul.adapters.pi_workspace_holder import (
    HolderProfile,
    allocate_workspace_holder,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="recovery intents require POSIX filesystem semantics")


class AllocationDocker(FakeDocker):
    def __init__(self, intent):
        super().__init__(intent)
        self.containers, self.volume, self.started = {}, None, False

    def __call__(self, *args):
        if args[:2] == ("volume", "create"):
            self.calls.append(args)
            self.volume = self.intent.labels("volume")
            return self.intent.volume_name
        if args[0] == "create":
            self.calls.append(args)
            self.containers[self.intent.holder_name] = ("b" * 64, self.intent.labels("holder"))
            return "b" * 64
        if args[0] == "start":
            self.calls.append(args)
            self.started = True
            return args[1]
        return super().__call__(*args)


def profile():
    return HolderProfile("docker.io/fixture@sha256:" + "a" * 64, "sha256:" + "b" * 64,
                         ("PATH=/usr/bin",), lambda: None)


def test_scheduler_denial_precedes_every_docker_operation(tmp_path):
    root, intent, _ = setup(tmp_path)
    control = AllocationDocker(intent)
    with pytest.raises(WorkspaceCleanupRequired):
        allocate_workspace_holder(root, intent.token, intent.fingerprint, control, profile(), lambda _: False)
    assert control.calls == [] and not control.containers and control.volume is None


def test_holder_allocation_requires_absent_names_and_rechecks_started_state(tmp_path, monkeypatch):
    root, intent, _ = setup(tmp_path)
    control, observed = AllocationDocker(intent), []
    monkeypatch.setattr(pi_workspace_holder, "verify_workspace_holder",
                        lambda *args, running: observed.append((running, control.started)))
    holder = allocate_workspace_holder(root, intent.token, intent.fingerprint, control, profile(),
                                       lambda fingerprint: fingerprint == intent.fingerprint)
    assert holder == "b" * 64 and observed == [(False, False), (True, True)]
    assert control.volume == intent.labels("volume")
    assert recover_workspace_lease(root, intent.token, intent.fingerprint, control).exists()


def test_interrupted_allocation_retains_intent_until_explicit_recovery(tmp_path, monkeypatch):
    root, intent, _ = setup(tmp_path)
    control = AllocationDocker(intent)

    def fail_verify(*args, **kwargs):
        raise ValueError("private runtime detail")

    monkeypatch.setattr(pi_workspace_holder, "verify_workspace_holder", fail_verify)
    with pytest.raises(WorkspaceCleanupRequired) as failure:
        allocate_workspace_holder(root, intent.token, intent.fingerprint, control, profile(), lambda _: True)
    assert "private runtime detail" not in str(failure.value)
    assert control.volume is not None and control.containers
    assert not (root / (intent.token + ".cleanup.json")).exists()
    assert recover_workspace_lease(root, intent.token, intent.fingerprint, control).exists()


@pytest.mark.parametrize("change", [
    {"CapAdd": ["SYS_ADMIN"]},
    {"IpcMode": "host"},
    {"CgroupnsMode": "host"},
    {"SecurityOpt": ["no-new-privileges:false"]},
    {"SecurityOpt": ["no-new-privileges", "seccomp=unconfined"]},
])
def test_holder_rejects_elevated_or_ambiguous_privileges(change):
    host = {"CapAdd": None, "IpcMode": "private", "UTSMode": "", "UsernsMode": "", "CgroupnsMode": "private",
            "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"]}
    host.update(change)
    with pytest.raises(ValueError, match="privileges"):
        pi_workspace_holder._verify_privileges(host)
