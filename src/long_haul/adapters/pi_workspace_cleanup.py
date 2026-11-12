"""Recover only Docker objects bound to an admitted workspace lease intent.

The Docker control callback is trusted and must target a locally authenticated
daemon. A successful cleanup marker is not production authorization or a
substitute for a scheduler-owned lease.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .pi_workspace_intent import load_workspace_intent, persist_workspace_cleanup

_CONTAINER = re.compile(r"[0-9a-f]{64}\Z")


class WorkspaceCleanupRequired(RuntimeError):
    def __init__(self):
        super().__init__("workspace cleanup is unconfirmed; retain the lease and private recovery intent")


def _daemon(control, expected):
    if control("info", "--format", "{{.ID}}").strip() != expected:
        raise ValueError("workspace cleanup daemon changed")


def _container(control, name, labels):
    output = control("container", "ls", "--all", "--no-trunc", "--filter", "name=^/" + name + "$",
                     "--format", "{{.ID}}")
    ids = output.splitlines()
    if not ids:
        return None
    if len(ids) != 1 or not _CONTAINER.fullmatch(ids[0]):
        raise ValueError("workspace container enumeration is ambiguous")
    info, = json.loads(control("inspect", ids[0]))
    actual = info["Config"].get("Labels") or {}
    if info["Id"] != ids[0] or info["Name"] != "/" + name or any(actual.get(k) != v for k, v in labels.items()):
        raise ValueError("workspace container identity does not match its lease")
    return ids[0]


def _volume(control, name, labels, options):
    output = control("volume", "ls", "--filter", "name=^" + name + "$", "--format", "{{.Name}}")
    names = output.splitlines()
    if not names:
        return None
    if names != [name]:
        raise ValueError("workspace volume enumeration is ambiguous")
    info, = json.loads(control("volume", "inspect", name))
    actual = info.get("Labels") or {}
    if (info["Name"] != name or info.get("Driver") != "local" or info.get("Scope") != "local"
            or info.get("Options") != {"type": "tmpfs", "device": "tmpfs", "o": options}
            or any(actual.get(k) != v for k, v in labels.items())):
        raise ValueError("workspace volume identity does not match its lease")
    return name


def _objects(control, intent):
    return (_container(control, intent.worker_name, intent.labels("worker")),
            _container(control, intent.holder_name, intent.labels("holder")),
            _volume(control, intent.volume_name, intent.labels("volume"), intent.volume_options))


def recover_workspace_lease(root: Path, token: str, expected_fingerprint: str, control):
    """Remove exact owned resources, verify absence, then sync a private marker."""
    try:
        intent = load_workspace_intent(root, token, expected_fingerprint)
        _daemon(control, intent.daemon_id)
        worker, holder, volume = _objects(control, intent)
        for identity in (worker, holder):
            if identity:
                _daemon(control, intent.daemon_id)
                control("rm", "--force", identity)
        if volume:
            _daemon(control, intent.daemon_id)
            if _volume(control, intent.volume_name, intent.labels("volume"), intent.volume_options) != volume:
                raise ValueError("workspace volume changed before removal")
            control("volume", "rm", volume)
        _daemon(control, intent.daemon_id)
        if any(_objects(control, intent)):
            raise ValueError("workspace resources still exist")
        _daemon(control, intent.daemon_id)
        return persist_workspace_cleanup(root, intent)
    except Exception:  # noqa: BLE001 -- provider errors may contain private identifiers.
        raise WorkspaceCleanupRequired() from None
