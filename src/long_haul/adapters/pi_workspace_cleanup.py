"""Recover only Docker objects bound to an admitted workspace lease intent.

The Docker control callback is trusted and must target a locally authenticated
daemon. A successful cleanup marker is not production authorization or a
substitute for a scheduler-owned lease.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path

from ..work.repository_sandbox import EVALUATOR_CANDIDATE_PREFIX
from .pi_workspace_intent import (
    _open_private_root,
    load_workspace_intent,
    persist_workspace_cleanup,
)

_CONTAINER = re.compile(r"[0-9a-f]{64}\Z")
MAX_SCRATCH_INSPECTION_NODES = 4096


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


def _prepare_scratch_tree(directory, budget, depth=0):
    if depth > 128 or budget[0] <= 0:
        raise ValueError("workspace scratch directory bound exceeded")
    budget[0] -= 1
    if os.fstat(directory).st_uid != os.geteuid():
        raise ValueError("workspace scratch contains an unowned directory")
    os.fchmod(directory, 0o700)
    with os.scandir(directory) as entries:
        for entry in entries:
            budget[0] -= 1
            if budget[0] < 0:
                raise ValueError("workspace scratch directory bound exceeded")
            if entry.name.startswith(EVALUATOR_CANDIDATE_PREFIX):
                raise ValueError("workspace contains an unresolved evaluator candidate")
            if entry.is_dir(follow_symlinks=False):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                try:
                    _prepare_scratch_tree(child, budget, depth + 1)
                finally:
                    os.close(child)


def _admitted_scratch(directory, intent):
    name = intent.token + ".scratch"
    try:
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ValueError("workspace scratch identity is not private")
    scratch = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        from .pi_workspace_fence import _matches

        opened = os.fstat(scratch)
        payload = (intent.token + ":" + intent.fingerprint).encode("ascii")
        if ((opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
                or not _matches(scratch, ".workspace-owner", payload)):
            raise ValueError("workspace scratch ownership is not admitted")
        return scratch
    except BaseException:
        os.close(scratch)
        raise


def _reject_evaluator_candidates(scratch, budget=None, depth=0):
    if budget is None:
        budget = [MAX_SCRATCH_INSPECTION_NODES]
    if depth > 128 or budget[0] <= 0 or os.fstat(scratch).st_uid != os.geteuid():
        raise ValueError("workspace scratch cannot be safely inspected")
    budget[0] -= 1
    with os.scandir(scratch) as entries:
        for entry in entries:
            budget[0] -= 1
            if budget[0] < 0:
                raise ValueError("workspace scratch inspection budget exceeded")
            if entry.name.startswith(EVALUATOR_CANDIDATE_PREFIX):
                raise ValueError("workspace contains an unresolved evaluator candidate")
            if entry.is_dir(follow_symlinks=False):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=scratch)
                try:
                    _reject_evaluator_candidates(child, budget, depth + 1)
                finally:
                    os.close(child)


def _check_scratch_before_recovery(root, intent):
    directory = _open_private_root(root)
    try:
        scratch = _admitted_scratch(directory, intent)
        if scratch is not None:
            try:
                _reject_evaluator_candidates(scratch)
            finally:
                os.close(scratch)
    finally:
        os.close(directory)


def _remove_scratch(root, intent):
    """Delete only the intent's private scratch via symlink-resistant dirfd APIs."""
    if not shutil.rmtree.avoids_symlink_attacks:
        raise ValueError("safe workspace scratch cleanup is unavailable")
    directory = _open_private_root(root)
    try:
        name = intent.token + ".scratch"
        scratch = _admitted_scratch(directory, intent)
        if scratch is None:
            return
        try:
            _reject_evaluator_candidates(scratch)
            _prepare_scratch_tree(scratch, [MAX_SCRATCH_INSPECTION_NODES])
        finally:
            os.close(scratch)
        shutil.rmtree(name, dir_fd=directory)
        os.fsync(directory)
    finally:
        os.close(directory)


def recover_workspace_lease(root: Path, token: str, expected_fingerprint: str, control, grant):
    """Remove exact owned resources under the exclusive local workspace fence."""
    try:
        from .pi_workspace_fence import WorkspaceFenceGrant

        if (not isinstance(grant, WorkspaceFenceGrant) or not grant.belongs(root, token, expected_fingerprint)):
            raise ValueError("workspace recovery lacks the held local fence")
        intent = load_workspace_intent(root, token, expected_fingerprint)
        _check_scratch_before_recovery(root, intent)
        _daemon(control, intent.daemon_id)
        worker, holder, volume = _objects(control, intent)
        for name, labels, identity in ((intent.worker_name, intent.labels("worker"), worker),
                                       (intent.holder_name, intent.labels("holder"), holder)):
            if identity:
                _daemon(control, intent.daemon_id)
                if _container(control, name, labels) != identity:
                    raise ValueError("workspace container changed before removal")
                control("rm", "--force", identity)
                _daemon(control, intent.daemon_id)
                if _container(control, name, labels) is not None:
                    raise ValueError("workspace container removal is unconfirmed")
        if volume:
            _daemon(control, intent.daemon_id)
            if _volume(control, intent.volume_name, intent.labels("volume"), intent.volume_options) != volume:
                raise ValueError("workspace volume changed before removal")
            control("volume", "rm", volume)
        _daemon(control, intent.daemon_id)
        if any(_objects(control, intent)):
            raise ValueError("workspace resources still exist")
        _daemon(control, intent.daemon_id)
        _remove_scratch(root, intent)
        if any(_objects(control, intent)):
            raise ValueError("workspace resources returned during scratch cleanup")
        _daemon(control, intent.daemon_id)
        return persist_workspace_cleanup(root, intent)
    except Exception:  # noqa: BLE001 -- provider errors may contain private identifiers.
        raise WorkspaceCleanupRequired() from None
