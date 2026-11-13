"""Bounded Docker-copy transport for the experimental workspace snapshot.

The caller must verify the exact container and hold its process tree paused or
stopped throughout this operation. This component does not establish either
property or release any worker lease.
"""
from __future__ import annotations

import os
import re
import selectors
import subprocess
import tempfile
import time
from pathlib import Path

from ..work.repository_sandbox import _control_environment
from .pi_workspace_archive import read_workspace_archive

_CONTAINER = re.compile(r"[0-9a-f]{64}\Z")


class _DeadlineReader:
    def __init__(self, stream, deadline):
        self.stream, self.deadline = stream, deadline
        self.selector = selectors.DefaultSelector()
        os.set_blocking(stream.fileno(), False)
        self.selector.register(stream, selectors.EVENT_READ)

    def read(self, size=-1):
        size = min(size if size >= 0 else 65536, 65536)
        if not size:
            return b""
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("workspace copy deadline exceeded")
            try:
                return os.read(self.stream.fileno(), size)
            except BlockingIOError:
                continue

    def close(self):
        self.selector.close()


def copy_docker_workspace(docker_path: Path, container: str, timeout_seconds: float):
    """Read a bounded archive, requiring a successful Docker-copy exit.

    Control output and workspace content are private. Failure never resumes a
    container or signifies successful cleanup; the owner retains that duty.
    """
    if not _CONTAINER.fullmatch(container) or not 0 < timeout_seconds <= 30:
        raise ValueError("invalid workspace copy request")
    deadline = time.monotonic() + timeout_seconds
    process = None
    with tempfile.TemporaryDirectory(prefix="long-haul-docker-config-") as config:
        try:
            process = subprocess.Popen(
                [str(docker_path), "--config", config, "cp", container + ":/workspace/.", "-"],
                env=_control_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            reader = _DeadlineReader(process.stdout, deadline)
            try:
                snapshot = read_workspace_archive(reader)
            finally:
                reader.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or process.wait(timeout=remaining) != 0:
                raise ValueError("workspace copy did not finish successfully")
            return snapshot
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("workspace copy failed or exceeded its deadline") from error
        finally:
            if process is not None:
                try:
                    if process.poll() is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass  # The client may have exited after poll().
                    process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    raise RuntimeError("workspace copy cleanup is unconfirmed; retain the lease") from None
                finally:
                    process.stdout.close()
