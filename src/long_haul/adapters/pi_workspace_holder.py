"""Experimental allocation of one isolated holder for a workspace lease.

The scheduler must durably admit the intent before allocation. The caller is
responsible for seed admission, worker lifecycle and recovery on every error.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .pi_workspace_cleanup import WorkspaceCleanupRequired, _daemon, _objects, _volume
from .pi_workspace_fence import WorkspaceFenceGrant
from .pi_workspace_intent import load_workspace_intent

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HOLDER_PROGRAM = "setInterval(()=>{},60000)"


@dataclass(frozen=True)
class HolderProfile:
    image: str
    image_id: str
    image_environment: tuple[str, ...]
    verify_runtime: Callable = field(repr=False)

    def __post_init__(self):
        if (not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", self.image)
                or not _DIGEST.fullmatch(self.image_id) or not callable(self.verify_runtime)
                or not isinstance(self.image_environment, tuple)
                or any(not isinstance(item, str) or "=" not in item for item in self.image_environment)
                or len({item.split("=", 1)[0] for item in self.image_environment}) != len(self.image_environment)):
            raise ValueError("invalid holder runtime profile")


def _arguments(intent, profile):
    labels = ["--label=" + key + "=" + value for key, value in intent.labels("holder").items()]
    return ["create", "--name", intent.holder_name, *labels, "--runtime=long-haul-gvisor", "--network=none",
            "--read-only", "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--pids-limit=64", "--memory=128m", "--memory-swap=128m", "--cpus=0.5", "--log-driver=none",
            "--mount=type=volume,src=" + intent.volume_name + ",dst=/workspace",
            "--entrypoint=/usr/local/bin/node", "--pull=never", profile.image, "-e", _HOLDER_PROGRAM]


def _verify_privileges(host):
    if (host.get("CapAdd") or host.get("IpcMode") == "host" or host.get("UTSMode") == "host"
            or host.get("UsernsMode") == "host" or host.get("CgroupnsMode") == "host"
            or "ALL" not in [cap.upper() for cap in host.get("CapDrop", [])]
            or len(host.get("SecurityOpt", [])) != 1
            or host["SecurityOpt"][0] not in {"no-new-privileges", "no-new-privileges:true"}):
        raise ValueError("workspace holder privileges differ from admitted configuration")


def _verify_configuration(info, intent, profile, holder_id, running):
    config, host, state = info["Config"], info["HostConfig"], info["State"]
    labels = config.get("Labels") or {}
    actual = (info["Id"], info["Name"], info["Image"], config["User"], config["Entrypoint"], config["Cmd"],
              state.get("Running"), state.get("Paused"), host["Runtime"], host["NetworkMode"], host["ReadonlyRootfs"],
              host["Memory"], host["MemorySwap"], host["PidsLimit"], host["NanoCpus"], host.get("Privileged", False),
              host.get("PidMode", ""), host.get("Tmpfs") or {}, host.get("LogConfig", {}).get("Type"),
              host.get("RestartPolicy", {}).get("Name"), host.get("AutoRemove"))
    expected = (holder_id, "/" + intent.holder_name, profile.image_id, "65534:65534", ["/usr/local/bin/node"],
                ["-e", _HOLDER_PROGRAM], running, False, "long-haul-gvisor", "none", True,
                128 * 1048576, 128 * 1048576, 64, 500000000, False, "", {}, "none", "no", False)
    if (actual != expected or any(labels.get(key) != value for key, value in intent.labels("holder").items())
            or sorted(config.get("Env", [])) != sorted(profile.image_environment)):
        raise ValueError("workspace holder profile differs from admitted configuration")
    _verify_privileges(host)


def verify_workspace_holder(control, intent, profile, holder_id, *, running):
    """Recheck the exact holder and volume before trusting its workspace."""
    profile.verify_runtime()
    _daemon(control, intent.daemon_id)
    _volume(control, intent.volume_name, intent.labels("volume"), intent.volume_options)
    info, = json.loads(control("inspect", holder_id))
    volume, = json.loads(control("volume", "inspect", intent.volume_name))
    _verify_configuration(info, intent, profile, holder_id, running)
    mounts = info["Mounts"]
    if (len(mounts) != 1 or mounts[0].get("Type") != "volume" or mounts[0].get("Name") != intent.volume_name
            or mounts[0].get("Destination") != "/workspace" or mounts[0].get("Source") != volume["Mountpoint"]
            or mounts[0].get("RW") is not True):
        raise ValueError("workspace holder has unexpected mounts")


def allocate_workspace_holder(root: Path, token, fingerprint, control, profile, grant):
    """Allocate after durable intent and scheduler admission; retain lease on error."""
    try:
        intent = load_workspace_intent(root, token, fingerprint)
        if (not isinstance(profile, HolderProfile) or not isinstance(grant, WorkspaceFenceGrant)
                or not grant.belongs(root, token, fingerprint) or not grant.admit(intent.fingerprint)):
            raise ValueError("workspace intent is not scheduler-admitted")
        if load_workspace_intent(root, token, fingerprint) != intent:
            raise ValueError("workspace intent changed after admission")
        profile.verify_runtime()
        _daemon(control, intent.daemon_id)
        if any(_objects(control, intent)):
            raise ValueError("workspace lease names are already allocated")
        labels = ["--label=" + key + "=" + value for key, value in intent.labels("volume").items()]
        control("volume", "create", *labels, "--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs",
                "--opt", "o=" + intent.volume_options, intent.volume_name)
        _daemon(control, intent.daemon_id)
        if _volume(control, intent.volume_name, intent.labels("volume"), intent.volume_options) is None:
            raise ValueError("workspace volume creation is unconfirmed")
        _daemon(control, intent.daemon_id)
        holder = control(*_arguments(intent, profile)).strip()
        if not re.fullmatch(r"[0-9a-f]{64}", holder):
            raise ValueError("workspace holder identity is invalid")
        verify_workspace_holder(control, intent, profile, holder, running=False)
        control("start", holder)
        verify_workspace_holder(control, intent, profile, holder, running=True)
        return holder
    except Exception:  # noqa: BLE001 -- retain the intent; provider errors can contain private data.
        raise WorkspaceCleanupRequired() from None
