"""Small Linux process boundary for the Qwen CPU pilot, inside an owned sandbox.

This enforces local limits; it does not acquire a Fleet lease, isolate networking,
or certify that the enclosing container/cgroup is empty after a failed attempt.
"""
from __future__ import annotations

import base64
import os
import selectors
import signal
import subprocess
import sys
import time
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProcessLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    wall_seconds: float = Field(strict=True, gt=0, le=3600, allow_inf_nan=False)
    cpu_seconds: int = Field(strict=True, ge=1, le=3600)
    address_space_bytes: int = Field(strict=True, ge=64 * 1024**2, le=64 * 1024**3)
    max_output_bytes: int = Field(strict=True, ge=256, le=16 * 1024**2)
    cpus: tuple[Annotated[int, Field(strict=True, ge=0)], ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cpus(self):
        if any(type(cpu) is not int or cpu < 0 for cpu in self.cpus):
            raise ValueError("CPU identifiers must be nonnegative integers")
        if len(set(self.cpus)) != len(self.cpus):
            raise ValueError("duplicate CPU identifiers")
        return self


class ProcessOutcome(BaseModel):
    outcome: str
    wall_seconds: float
    exit_code: int | None = None
    stdout_base64: str = ""
    stderr_base64: str = ""
    leader_reaped: bool = False
    group_kill_sent: bool = False

    @property
    def stdout(self) -> bytes:
        return base64.b64decode(self.stdout_base64)

    @property
    def stderr(self) -> bytes:
        return base64.b64decode(self.stderr_base64)


# Run isolated stdlib Python before exec, avoiding a threaded-parent preexec_fn.
# The qualified runtime must bind this Python interpreter as part of its closure.
_BOOTSTRAP = """
import json, os, resource, sys
limits = json.loads(sys.argv[1])
resource.setrlimit(resource.RLIMIT_AS, (limits['address_space_bytes'],) * 2)
resource.setrlimit(resource.RLIMIT_CPU, (limits['cpu_seconds'],) * 2)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
os.sched_setaffinity(0, limits['cpus'])
os.execve(sys.argv[2], sys.argv[2:], dict(os.environ))
"""


def _collect(process, limits, deadline):
    output = [bytearray(), bytearray()]
    with selectors.DefaultSelector() as selector:
        for stream, index in ((process.stdout, 0), (process.stderr, 1)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, index)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", output
            for key, _ in selector.select(min(.05, remaining)):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                available = limits.max_output_bytes - sum(map(len, output))
                output[key.data].extend(chunk[:available])
                if len(chunk) > available:
                    return "output_limit", output
    try:
        process.wait(timeout=max(.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return "timeout", output
    return ("completed" if process.returncode == 0 else "nonzero_exit"), output


def _stop_group(process):
    sent = False
    try:
        os.killpg(process.pid, signal.SIGKILL)
        sent = True
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return sent, False
    return sent, True


def run_process(command: list[str], limits: ProcessLimits) -> ProcessOutcome:
    """Bound output, wall/CPU time, address space and affinity; reap the leader.

    Every exit path kills remaining process-group members. Descendants can escape
    a process group, so an external container/cgroup owner must still verify
    stopped-workload evidence before releasing its reservation. This is not a
    hostile-code sandbox or an admission grant.
    """
    started = time.monotonic()
    outcome, output, process = "launch_failed", [b"", b""], None
    sent, reaped = False, False
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _BOOTSTRAP,
             limits.model_dump_json(), *command],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"}, start_new_session=True,
        )
        outcome, output = _collect(process, limits, started + limits.wall_seconds)
    except OSError:
        outcome = "launch_failed" if process is None else "capture_failed"
    finally:
        if process is not None:
            sent, reaped = _stop_group(process)
            process.stdout.close()
            process.stderr.close()
    return ProcessOutcome(
        outcome=outcome if process is None or reaped else "cleanup_failed",
        wall_seconds=time.monotonic() - started,
        exit_code=None if process is None else process.returncode,
        stdout_base64=base64.b64encode(output[0]).decode("ascii"),
        stderr_base64=base64.b64encode(output[1]).decode("ascii"),
        leader_reaped=reaped, group_kill_sent=sent,
    )
