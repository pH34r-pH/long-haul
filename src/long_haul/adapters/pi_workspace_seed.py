"""Build a bounded seed archive from previously admitted worker input.

This component does not authorize the input, create a volume or launch a
worker. The proposed quota-backed workspace is not yet integrated.
"""
from __future__ import annotations

import io
import tarfile

from ..work.worker_candidate import (
    MAX_WORKER_DEPTH,
    MAX_WORKER_DIRECTORIES,
    MAX_WORKER_FILE_BYTES,
    MAX_WORKER_FILES,
    MAX_WORKER_TOTAL_BYTES,
    worker_path,
)
from .pi_workspace_archive import MAX_ARCHIVE_BYTES


def _seed_directories(targets):
    if len(targets) > MAX_WORKER_FILES:
        raise ValueError("workspace seed exceeds target count")
    directories = set()
    for path in targets:
        parts = worker_path(path).split("/")
        if len(parts) > MAX_WORKER_DEPTH:
            raise ValueError("workspace seed exceeds directory depth")
        directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    if len(directories) > MAX_WORKER_DIRECTORIES or directories.intersection(targets):
        raise ValueError("workspace seed has too many or conflicting directories")
    return directories


def _add(archive, name, *, data=None):
    info = tarfile.TarInfo(name)
    info.uid = info.gid = 65534
    if data is None:
        info.type, info.mode = tarfile.DIRTYPE, 0o777
        archive.addfile(info)
    else:
        info.mode, info.size = 0o666, len(data)
        archive.addfile(info, io.BytesIO(data))


def build_workspace_seed(targets, contents):
    """Build deterministic regular-file/dir entries; no filesystem is read."""
    targets = set(targets)
    contents = dict(contents)
    directories = _seed_directories(targets)
    if not set(contents) <= targets:
        raise ValueError("workspace seed includes an unadmitted target")
    total = 0
    for value in contents.values():
        if not isinstance(value, bytes) or len(value) > MAX_WORKER_FILE_BYTES:
            raise ValueError("workspace seed exceeds file bound")
        total += len(value)
        if total > MAX_WORKER_TOTAL_BYTES:
            raise ValueError("workspace seed exceeds total bound")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for directory in sorted(directories):
            _add(archive, directory)
        for path in sorted(contents):
            _add(archive, path, data=contents[path])
    if output.tell() > MAX_ARCHIVE_BYTES:
        raise ValueError("workspace seed archive exceeds byte bound")
    return output.getvalue()
