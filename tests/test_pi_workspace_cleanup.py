"""Adversarial recovery tests; the executor has not adopted this seam yet."""
import json
import os
import subprocess

import pytest

from long_haul.adapters.pi_workspace_cleanup import (
    WorkspaceCleanupRequired,
    recover_workspace_lease,
)
from long_haul.adapters.pi_workspace_intent import (
    WorkspaceLeaseIntent,
    persist_workspace_intent,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="recovery intents require POSIX filesystem semantics")


class FakeDocker:
    def __init__(self, intent):
        self.intent, self.daemon, self.calls = intent, intent.daemon_id, []
        self.containers = {intent.worker_name: ("a" * 64, intent.labels("worker")),
                           intent.holder_name: ("b" * 64, intent.labels("holder"))}
        self.volume = intent.labels("volume")
        self.volume_options = intent.volume_options
        self.fail = None

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == self.fail:
            raise subprocess.CalledProcessError(1, args, output=b"private provider data")
        if args[0] == "info":
            return self.daemon
        if args[:2] == ("container", "ls"):
            name = args[5].removeprefix("name=^/").removesuffix("$")
            entry = self.containers.get(name)
            return entry[0] + "\n" if entry else ""
        if args[0] == "inspect":
            identity = args[1]
            name, entry = next((name, entry) for name, entry in self.containers.items() if entry[0] == identity)
            return json.dumps([{"Id": identity, "Name": "/" + name, "Config": {"Labels": entry[1]}}])
        if args[:2] == ("volume", "ls"):
            return self.intent.volume_name + "\n" if self.volume is not None else ""
        if args[:2] == ("volume", "inspect"):
            return json.dumps([{"Name": self.intent.volume_name, "Labels": self.volume, "Driver": "local", "Scope": "local",
                                "Options": {"type": "tmpfs", "device": "tmpfs", "o": self.volume_options}}])
        if args[0] == "rm":
            identity = args[2]
            self.containers = {name: entry for name, entry in self.containers.items() if entry[0] != identity}
            return ""
        if args[:2] == ("volume", "rm"):
            self.volume = None
            return ""
        raise AssertionError(args)


def setup(tmp_path):
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    intent = WorkspaceLeaseIntent(token="c" * 32, attempt_fingerprint="sha256:" + "d" * 64,
                                  environment_fingerprint="sha256:" + "e" * 64, daemon_id="fixture-daemon")
    persist_workspace_intent(root, intent)
    return root, intent, FakeDocker(intent)


def recover(root, intent, control):
    return recover_workspace_lease(root, intent.token, intent.fingerprint, control)


def test_recovery_removes_owned_worker_holder_then_volume_and_syncs_marker(tmp_path):
    root, intent, docker = setup(tmp_path)
    marker = recover(root, intent, docker)
    removals = [call[:2] for call in docker.calls if call[0] == "rm" or call[:2] == ("volume", "rm")]
    assert removals == [("rm", "--force"), ("rm", "--force"), ("volume", "rm")]
    assert not docker.containers and docker.volume is None
    assert marker.read_text() == '{"intent":"' + intent.fingerprint + '","state":"absent"}'
    assert marker.stat().st_mode & 0o777 == 0o600
    assert recover(root, intent, docker) == marker  # Rechecks absence before reusing the marker.


@pytest.mark.parametrize("change", ["daemon", "worker-label", "holder-label", "volume-label", "volume-options", "fingerprint"])
def test_recovery_refuses_foreign_or_unadmitted_objects_before_mutation(tmp_path, change):
    root, intent, docker = setup(tmp_path)
    fingerprint = intent.fingerprint
    if change == "daemon":
        docker.daemon = "different-daemon"
    elif change == "fingerprint":
        fingerprint = "sha256:" + "f" * 64
    elif change == "volume-label":
        docker.volume["dev.ph34r.long-haul.lease"] = "other"
    elif change == "volume-options":
        docker.volume_options = "size=1g"
    else:
        name = intent.worker_name if change == "worker-label" else intent.holder_name
        docker.containers[name][1]["dev.ph34r.long-haul.lease"] = "other"
    with pytest.raises(WorkspaceCleanupRequired) as failure:
        recover_workspace_lease(root, intent.token, fingerprint, docker)
    assert intent.token not in str(failure.value)
    assert not any(call[0] == "rm" or call[:2] == ("volume", "rm") for call in docker.calls)
    assert not (root / (intent.token + ".cleanup.json")).exists()


def test_recovery_retries_partial_failure_without_claiming_cleanup(tmp_path):
    root, intent, docker = setup(tmp_path)
    docker.fail = "rm"
    with pytest.raises(WorkspaceCleanupRequired):
        recover(root, intent, docker)
    assert len(docker.containers) == 2 and docker.volume is not None
    assert not (root / (intent.token + ".cleanup.json")).exists()
    docker.fail = None
    assert recover(root, intent, docker).exists()


@pytest.mark.parametrize("change", ["daemon", "volume"])
def test_recovery_stops_when_daemon_or_volume_changes_mid_cleanup(tmp_path, change):
    root, intent, docker = setup(tmp_path)

    def control(*args):
        result = docker(*args)
        if args[:2] == ("rm", "--force") and args[2] == "b" * 64:
            if change == "daemon":
                docker.daemon = "different-daemon"
            else:
                docker.volume["dev.ph34r.long-haul.lease"] = "foreign"
        return result

    with pytest.raises(WorkspaceCleanupRequired):
        recover(root, intent, control)
    assert docker.volume is not None
    assert not any(call[:2] == ("volume", "rm") for call in docker.calls)
    assert not (root / (intent.token + ".cleanup.json")).exists()


def test_tampered_cleanup_marker_cannot_release_lease(tmp_path):
    root, intent, docker = setup(tmp_path)
    marker = recover(root, intent, docker)
    marker.write_text("changed")
    with pytest.raises(WorkspaceCleanupRequired):
        recover(root, intent, docker)
