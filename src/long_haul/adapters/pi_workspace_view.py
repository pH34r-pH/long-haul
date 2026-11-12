"""Experimental paused-volume view for trusted executor-owned interlocks.

The caller must verify the full worker and holder images, runtime profiles and
control-plane ownership. This component does not create those resources or
perform their durable recovery and is not yet connected to the Pi worker.
"""
from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ..work.worker_candidate import MAX_WORKER_FILE_BYTES, MAX_WORKER_TOTAL_BYTES
from .pi_workspace_copy import copy_docker_workspace
from .pi_workspace_seed import _seed_directories

_CONTAINER = re.compile(r"[0-9a-f]{64}\Z")
_VOLUME = re.compile(r"lh-workspace-[0-9a-f]{32}\Z")
_OPTIONS = re.compile(r"size=([1-8])m,nr_inodes=([1-9][0-9]{0,3}),mode=0777,nosuid,nodev,noexec\Z")


def _mount(info, volume, mountpoint):
    mounts = [item for item in info["Mounts"] if item["Destination"] == "/workspace"]
    if (len(mounts) != 1 or mounts[0].get("Type") != "volume" or mounts[0].get("Name") != volume
            or mounts[0].get("Source") != mountpoint or mounts[0].get("RW") is not True):
        raise ValueError("workspace volume identity changed")


def _materialize(root, files):
    directories = _seed_directories(set(files))
    total = 0
    for data in files.values():
        if not isinstance(data, bytes) or len(data) > MAX_WORKER_FILE_BYTES:
            raise ValueError("workspace view exceeds file bound")
        total += len(data)
        if total > MAX_WORKER_TOTAL_BYTES:
            raise ValueError("workspace view exceeds total bound")
    for path in sorted(directories):
        (root / path).mkdir(mode=0o700, parents=True, exist_ok=True)
    for path, data in sorted(files.items()):
        (root / path).write_bytes(data)
        (root / path).chmod(0o600)


@dataclass(frozen=True)
class PausedWorkspaceView:
    control: Callable = field(repr=False)
    docker_path: Path
    worker: str
    holder: str
    volume: str
    options: str
    verify: Callable = field(repr=False)

    def __post_init__(self):
        options = _OPTIONS.fullmatch(self.options)
        if (not _CONTAINER.fullmatch(self.worker) or not _CONTAINER.fullmatch(self.holder) or self.worker == self.holder
                or not _VOLUME.fullmatch(self.volume) or not callable(self.verify) or not callable(self.control)
                or options is None or int(options.group(2)) > 1024):
            raise ValueError("invalid paused workspace binding")

    def _check(self, *, paused):
        self.verify()  # Full executor-owned profile, image and control-plane binding.
        worker, holder = json.loads(self.control("inspect", self.worker, self.holder))
        volume, = json.loads(self.control("volume", "inspect", self.volume))
        if (worker["Id"] != self.worker or holder["Id"] != self.holder
                or worker["State"].get("Running") is not True or worker["State"].get("Paused") is not paused
                or holder["State"].get("Running") is not True or holder["State"].get("Paused") is not False
                or volume.get("Name") != self.volume or volume.get("Driver") != "local"
                or volume.get("Scope") != "local"
                or volume.get("Options") != {"type": "tmpfs", "device": "tmpfs", "o": self.options}):
            raise ValueError("paused workspace binding changed")
        _mount(worker, self.volume, volume["Mountpoint"])
        _mount(holder, self.volume, volume["Mountpoint"])

    @contextmanager
    def __call__(self):
        # A failure before unpause leaves the worker paused. Any error requires
        # the owner to stop and recover it; the resume outcome may be uncertain.
        self._check(paused=False)
        self.control("pause", self.worker)
        self._check(paused=True)
        files = copy_docker_workspace(self.docker_path, self.holder, 15)
        self._check(paused=True)
        with tempfile.TemporaryDirectory(prefix="long-haul-workspace-view-") as temporary:
            root = Path(temporary)
            _materialize(root, files)
            yield root
            self._check(paused=True)
        self.control("unpause", self.worker)
        self._check(paused=False)
