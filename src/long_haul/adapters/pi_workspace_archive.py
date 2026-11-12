"""Validate a Docker workspace archive without extracting untrusted entries.

This is a preliminary component, not a qualified worker boundary. The caller
must pause the exact container, bound the subprocess lifetime, and confirm the
Docker copy's successful exit before using the returned snapshot.
"""
from __future__ import annotations

import tarfile

from ..work.worker_candidate import (
    MAX_WORKER_DEPTH,
    MAX_WORKER_DIRECTORIES,
    MAX_WORKER_FILE_BYTES,
    MAX_WORKER_FILES,
    MAX_WORKER_TOTAL_BYTES,
    worker_path,
)

# Bounds headers and PAX metadata as well as file contents. It is deliberately
# independent of the size reported by untrusted tar entries.
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024


class _BoundedStream:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = MAX_ARCHIVE_BYTES
        self.tail = b""
        self.history = b""
        self.count = 0

    def read(self, size=-1):
        if size < 0:
            size = self.remaining + 1
        data = self.stream.read(min(size, self.remaining + 1))
        if len(data) > self.remaining:
            raise ValueError("workspace archive exceeds byte limit")
        self.remaining -= len(data)
        self.count += len(data)
        self.tail = (self.tail + data)[-1024:]
        self.history = (self.history + data)[-65536:]
        return data

    def block_at(self, offset):
        start = offset - (self.count - len(self.history))
        if start < 0 or start + 512 > len(self.history):
            raise ValueError("workspace archive terminator is unavailable")
        return self.history[start:start + 512]


def _entry_path(member):
    name = member.name
    if name == ".":
        if not member.isdir():
            raise ValueError("workspace archive root is not a directory")
        return None
    if not name.startswith("./"):
        raise ValueError("workspace archive entry escaped its root")
    path = worker_path(name[2:])
    if len(path.split("/")) > MAX_WORKER_DEPTH:
        raise ValueError("workspace archive exceeds directory depth")
    return path


def _read_entry(archive, member):
    if (not member.isfile() or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
            or member.sparse is not None or member.size < 0 or member.size > MAX_WORKER_FILE_BYTES):
        raise ValueError("workspace archive contains an unsafe or oversized file")
    source = archive.extractfile(member)
    if source is None:
        raise ValueError("workspace archive file is unavailable")
    data = source.read(MAX_WORKER_FILE_BYTES + 1)
    if len(data) != member.size:
        raise ValueError("workspace archive file is truncated")
    return data


class _Snapshot:
    def __init__(self):
        self.files, self.directories, self.seen = {}, set(), set()
        self.root_seen = False
        self.total = 0

    def accept(self, archive, member):
        path = _entry_path(member)
        if path is None:
            if self.root_seen:
                raise ValueError("duplicate workspace archive root")
            self.root_seen = True
            return
        if not self.root_seen or path in self.seen:
            raise ValueError("workspace archive has an unexpected or duplicate entry")
        self.seen.add(path)
        if member.isdir():
            self.directories.add(path)
            if len(self.directories) > MAX_WORKER_DIRECTORIES:
                raise ValueError("workspace archive exceeds directory count")
            return
        if len(self.files) >= MAX_WORKER_FILES or self.total + member.size > MAX_WORKER_TOTAL_BYTES:
            raise ValueError("workspace archive exceeds candidate bounds")
        data = _read_entry(archive, member)
        self.files[path] = data
        self.total += len(data)

    def finish(self):
        if not self.root_seen:
            raise ValueError("workspace archive lacks its root")
        for path in self.seen:
            parents = path.split("/")[:-1]
            if any("/".join(parents[:index]) in self.files for index in range(1, len(parents) + 1)):
                raise ValueError("workspace archive nests an entry beneath a file")
            if any("/".join(parents[:index]) not in self.directories for index in range(1, len(parents) + 1)):
                raise ValueError("workspace archive omits a parent directory")
        return self.files


def read_workspace_archive(stream):
    """Return bounded regular bytes from ``docker cp container:/workspace/. -``.

    Never extracts paths to the host. Inputs, output and exceptions are private;
    the snapshot must not be trusted unless the surrounding copy succeeds.
    """
    snapshot, bounded = _Snapshot(), _BoundedStream(stream)
    try:
        with tarfile.open(fileobj=bounded, mode="r|") as archive:
            for member in archive:
                snapshot.accept(archive, member)
            # tarfile treats an invalid subsequent header like end-of-archive.
            # Confirm that its exact stopping block really is a zero block.
            if bounded.block_at(archive.offset) != b"\0" * 512:
                raise ValueError("workspace archive has an invalid header")
            # Drain the tar reader, including its internal read-ahead buffer.
            # Only zero padding may follow the first end-of-archive block.
            while padding := archive.fileobj.read(65536):
                if any(padding):
                    raise ValueError("workspace archive has data after its terminator")
    except (tarfile.TarError, EOFError, OSError) as error:
        raise ValueError("invalid or truncated workspace archive") from error
    if bounded.count % 512 or bounded.tail != b"\0" * 1024:
        raise ValueError("workspace archive lacks a complete terminator")
    return snapshot.finish()
