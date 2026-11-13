"""Private, pre-allocation recovery intent for a quota-backed worker.

The executor must persist this intent before creating any named Docker object.
The scheduler owns the private recovery directory and the admitted fingerprint;
the file alone is not authority to remove resources or release a lease.
"""
from __future__ import annotations

import os
import re
import secrets
import stat
from pathlib import Path

from pydantic import Field, field_validator

from ..work.repository_models import FrozenProjection


class WorkspaceLeaseIntent(FrozenProjection):
    token: str = Field(pattern=r"^[0-9a-f]{32}$", repr=False)
    attempt_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", repr=False)
    environment_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", repr=False)
    daemon_id: str = Field(pattern=r"^[A-Za-z0-9:._-]{1,128}$", repr=False)
    volume_options: str = "size=8m,nr_inodes=1024,mode=0777,nosuid,nodev,noexec"

    @field_validator("volume_options")
    @classmethod
    def bounded_volume(cls, value):
        match = re.fullmatch(r"size=[1-8]m,nr_inodes=([1-9][0-9]{0,3}),mode=0777,nosuid,nodev,noexec", value)
        if match is None or int(match.group(1)) > 1024:
            raise ValueError("workspace volume must have bounded tmpfs options")
        return value

    @classmethod
    def generate(cls, attempt_fingerprint, environment_fingerprint, daemon_id):
        return cls(token=secrets.token_hex(16), attempt_fingerprint=attempt_fingerprint,
                   environment_fingerprint=environment_fingerprint, daemon_id=daemon_id)

    @property
    def worker_name(self):
        return "lh-worker-" + self.token

    @property
    def holder_name(self):
        return "lh-holder-" + self.token

    @property
    def volume_name(self):
        return "lh-workspace-" + self.token

    def labels(self, role):
        if role not in {"worker", "holder", "volume"}:
            raise ValueError("invalid workspace lease role")
        return {"dev.ph34r.long-haul.lease": self.token,
                "dev.ph34r.long-haul.intent": self.fingerprint,
                "dev.ph34r.long-haul.attempt": self.attempt_fingerprint,
                "dev.ph34r.long-haul.environment": self.environment_fingerprint,
                "dev.ph34r.long-haul.daemon": self.daemon_id,
                "dev.ph34r.long-haul.role": role}


def _open_private_root(root: Path):
    if os.name != "posix" or not root.is_absolute() or root.resolve() != root:
        raise ValueError("workspace recovery root must be an absolute non-symlink POSIX directory")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened, named = os.fstat(descriptor), root.lstat()
        if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid() or opened.st_mode & 0o077
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
            raise ValueError("workspace recovery root is not private and stable")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def persist_workspace_intent(root: Path, intent: WorkspaceLeaseIntent):
    """Create and sync an immutable intent before any Docker allocation."""
    if not isinstance(intent, WorkspaceLeaseIntent):
        raise TypeError("workspace recovery requires an exact lease intent")
    directory = _open_private_root(root)
    try:
        name = intent.token + ".json"
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as output:
            output.write(intent.model_dump_json().encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
    return root / name


def load_workspace_intent(root: Path, token: str, expected_fingerprint: str):
    """Load only a private, singly linked intent admitted by trusted control."""
    if not isinstance(token, str) or len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
        raise ValueError("invalid workspace recovery token")
    directory = _open_private_root(root)
    try:
        descriptor = os.open(token + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1 or metadata.st_mode & 0o077 or metadata.st_size > 16384):
                raise ValueError("workspace recovery intent is not private")
            intent = WorkspaceLeaseIntent.model_validate_json(source.read(16385))
    finally:
        os.close(directory)
    if intent.token != token or intent.fingerprint != expected_fingerprint:
        raise ValueError("workspace recovery intent is not admitted")
    return intent


def persist_workspace_cleanup(root: Path, intent: WorkspaceLeaseIntent):
    """Sync an idempotent cleanup marker only after a fresh absence check."""
    name = intent.token + ".cleanup.json"
    payload = ("{\"intent\":\"" + intent.fingerprint + "\",\"state\":\"absent\"}").encode("ascii")
    directory = _open_private_root(root)
    try:
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        except FileExistsError:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as source:
                metadata = os.fstat(source.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                        or metadata.st_nlink != 1 or metadata.st_mode & 0o077 or source.read(4097) != payload):
                    raise ValueError("workspace cleanup marker is not exact and private")
                os.fsync(source.fileno())
        else:
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)
    return root / name
