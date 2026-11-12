"""Local, crash-persistent single-use fence for one scheduler-owned workspace.

All controllers for the same workspace must share the same private, local
recovery root. This does not fence a remote daemon from other hosts and does
not authenticate the scheduler or replace its durable delivery lease.
"""
from __future__ import annotations

import os
import re
import stat
import threading
from contextlib import contextmanager
from pathlib import Path

if os.name == "posix":
    import fcntl

from .pi_workspace_cleanup import WorkspaceCleanupRequired, recover_workspace_lease
from .pi_workspace_intent import _open_private_root, load_workspace_intent


def _private_file(directory, name, flags):
    descriptor = os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    metadata = os.fstat(descriptor)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1 or metadata.st_mode & 0o077):
        os.close(descriptor)
        raise ValueError("workspace fence file is not private")
    return descriptor


def _write_once(directory, name, payload):
    descriptor = _private_file(directory, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.fsync(directory)


def _matches(directory, name, payload):
    descriptor = _private_file(directory, name, os.O_RDONLY)
    with os.fdopen(descriptor, "rb") as source:
        return source.read(4097) == payload


class WorkspaceFenceGrant:
    """Valid only while the owning local controller holds the workspace lock."""

    def __init__(self, fence, directory, lock):
        self._fence, self._directory, self._lock = fence, directory, lock
        self._pid, self._seed_digest, self._seeded = os.getpid(), None, False
        self._thread = threading.get_ident()
        self._worker_started, self._closed = False, False

    def _assert_current(self):
        if self._closed or os.getpid() != self._pid or threading.get_ident() != self._thread:
            raise ValueError("workspace fence is no longer held by its owner")
        locked = os.fstat(self._lock)
        named = os.stat("workspace.lock", dir_fd=self._directory, follow_symlinks=False)
        if ((locked.st_dev, locked.st_ino) != (named.st_dev, named.st_ino)
                or not _matches(self._directory, "active.json", self._fence._active_payload())):
            raise ValueError("workspace fence identity changed")
        load_workspace_intent(self._fence.root, self._fence.token, self._fence.fingerprint)

    def admit(self, fingerprint):
        self._assert_current()
        return fingerprint == self._fence.fingerprint and self._seed_digest is None and not self._worker_started

    def belongs(self, root, token, fingerprint):
        self._assert_current()
        return (root, token, fingerprint) == (self._fence.root, self._fence.token, self._fence.fingerprint)

    def begin_seed(self, digest):
        self._assert_current()
        if (self._seed_digest is not None or self._worker_started or not isinstance(digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None):
            raise ValueError("workspace seed is not single-use")
        _write_once(self._directory, self._fence.token + ".seed-started", b"seed-started")
        self._seed_digest = digest

    def admit_seed(self, digest):
        self._assert_current()
        return self._seed_digest is not None and not self._seeded and digest == self._seed_digest

    def finish_seed(self, digest):
        if not self.admit_seed(digest):
            raise ValueError("workspace seed does not match the held fence")
        _write_once(self._directory, self._fence.token + ".seeded", b"seeded")
        self._seeded = True

    def begin_worker(self):
        self._assert_current()
        if not self._seeded or self._worker_started:
            raise ValueError("workspace is not ready for a single worker")
        _write_once(self._directory, self._fence.token + ".worker-started", b"worker-started")
        self._worker_started = True

    def cleanup(self, control):
        self._assert_current()
        marker = recover_workspace_lease(self._fence.root, self._fence.token, self._fence.fingerprint, control, self)
        self._fence._clear_active(self._directory)
        self._closed = True
        return marker


class LocalWorkspaceFence:
    def __init__(self, root: Path, token: str, fingerprint: str):
        self.root, self.token, self.fingerprint = root, token, fingerprint

    def _active_payload(self):
        return (self.token + ":" + self.fingerprint).encode("ascii")

    @contextmanager
    def _locked(self):
        directory = _open_private_root(self.root)
        lock = None
        try:
            lock = _private_file(directory, "workspace.lock", os.O_RDWR | os.O_CREAT)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fsync(lock)
            os.fsync(directory)
            yield directory, lock
        finally:
            if lock is not None:
                os.close(lock)
            os.close(directory)

    def _clear_active(self, directory):
        if not _matches(directory, "active.json", self._active_payload()):
            raise ValueError("workspace active marker is not admitted")
        os.unlink("active.json", dir_fd=directory)
        os.fsync(directory)

    @contextmanager
    def launch(self):
        with self._locked() as (directory, lock):
            load_workspace_intent(self.root, self.token, self.fingerprint)
            try:
                descriptor = _private_file(directory, "active.json", os.O_RDONLY)
            except FileNotFoundError:
                pass
            else:
                os.close(descriptor)
                raise WorkspaceCleanupRequired()
            _write_once(directory, self.token + ".used", b"used")
            _write_once(directory, "active.json", self._active_payload())
            grant = WorkspaceFenceGrant(self, directory, lock)
            try:
                yield grant
                if not grant._closed:
                    raise WorkspaceCleanupRequired()
            finally:
                grant._closed = True

    def recover(self, control):
        try:
            with self._locked() as (directory, lock):
                load_workspace_intent(self.root, self.token, self.fingerprint)
                if not _matches(directory, "active.json", self._active_payload()):
                    raise WorkspaceCleanupRequired()
                grant = WorkspaceFenceGrant(self, directory, lock)
                try:
                    return grant.cleanup(control)
                finally:
                    grant._closed = True
        except Exception:  # noqa: BLE001 -- retain the fence and hide private provider data.
            raise WorkspaceCleanupRequired() from None
