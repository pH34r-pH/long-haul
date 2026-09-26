"""llama.cpp CLI adapter. Its flags stay wholly at this edge."""
from __future__ import annotations

import hashlib
import math
import os
import selectors
import signal
import stat
import subprocess
import time
from pathlib import Path

from ..models import InferenceProfile
from ..runtime import (
    ExecutionRequest,
    ExecutionResult,
    FailureClass,
    ProfileValidation,
    RuntimeIdentity,
    Timing,
    ValidationDepth,
    ValidationState,
)

# Only the identity subprocess is covered by these limits. Model inference has
# its own request timeout. These are probe budgets, not dependency version pins.
IDENTITY_TIMEOUT_SECONDS = 5.0
IDENTITY_OUTPUT_BYTES = 16 * 1024
MAX_BINARY_BYTES = 512 * 1024 * 1024


def _version_probe(binary: Path, timeout: float) -> tuple[str, str | None]:
    """Read bounded combined output from a fixed --version argv on POSIX.

    No shell, model input, environment dump or retained failed-process output.
    Ordinary process groups are cleaned up even if a child holds the pipe open.
    This is a resource-bounded probe, not a sandbox for an untrusted executable.
    """
    if os.name != "posix":
        return "unsupported-probe-platform", None
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(
            [str(binary), "--version"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
    except FileNotFoundError:
        return "missing", None
    except PermissionError:
        return "denied", None
    except OSError:
        return "unusable", None
    try:
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout", None
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, min(4096, IDENTITY_OUTPUT_BYTES + 1 - len(output)))
                    if chunk:
                        output.extend(chunk)
                        if len(output) > IDENTITY_OUTPUT_BYTES:
                            return "output-limit", None
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return "timeout", None
                        try:
                            code = process.wait(timeout=remaining)
                        except subprocess.TimeoutExpired:
                            return "timeout", None
                        if code != 0:
                            return "unusable", None
                        try:
                            version = output.decode("utf-8").strip()
                        except UnicodeDecodeError:
                            return "unknown-version", None
                        if not version or any(ord(c) < 32 and c not in "\n\r\t" for c in version):
                            return "unknown-version", None
                        return "ready", version
    except (OSError, ValueError):
        return "unusable", None
    finally:
        # Kill the session's process group, not just the leader: a wrapper may
        # have exited while a child still holds stdout. No outside process group
        # is targeted. Escaped processes are outside this probe's guarantees.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.stdout is not None:
            process.stdout.close()
        process.wait()


class LlamaCppAdapter:
    runtime_id = "llama.cpp"

    def __init__(self, binary: str | Path, *, identity_timeout_seconds: float = IDENTITY_TIMEOUT_SECONDS):
        if (isinstance(identity_timeout_seconds, bool)
                or not math.isfinite(identity_timeout_seconds)
                or not 0 < identity_timeout_seconds <= IDENTITY_TIMEOUT_SECONDS):
            raise ValueError("identity timeout must be finite, positive and at most five seconds")
        # Path('./llama-cli') drops './'; use an absolute path so the version
        # probe and execution address the selected file rather than search PATH.
        self.binary = Path(binary).absolute()
        self.identity_timeout_seconds = identity_timeout_seconds

    def identity(self) -> RuntimeIdentity:
        def unavailable(status: str) -> RuntimeIdentity:
            return RuntimeIdentity(
                runtime_id=self.runtime_id, binary_path=str(self.binary),
                capabilities={"identity_probe": status, "resident": False},
            )

        try:
            before = self.binary.stat()
            if not stat.S_ISREG(before.st_mode):
                return unavailable("unusable")
            if not os.access(self.binary, os.X_OK):
                return unavailable("denied")
            if before.st_size > MAX_BINARY_BYTES:
                return unavailable("binary-size-limit")
            digest = hashlib.sha256()
            with self.binary.open("rb") as stream:
                read_bytes = 0
                while chunk := stream.read(1024 * 1024):
                    read_bytes += len(chunk)
                    if read_bytes > MAX_BINARY_BYTES:
                        return unavailable("binary-size-limit")
                    digest.update(chunk)
            status, version = _version_probe(self.binary, self.identity_timeout_seconds)
            if status != "ready":
                return unavailable(status)
            after = self.binary.stat()
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                return unavailable("binary-changed")
        except FileNotFoundError:
            return unavailable("missing")
        except PermissionError:
            return unavailable("denied")
        except OSError:
            return unavailable("unusable")
        return RuntimeIdentity(
            runtime_id=self.runtime_id, version=version, build_id=digest.hexdigest()[:16],
            binary_path=str(self.binary), backends=["cpu"],
            capabilities={"resident": True, "identity_probe": "ready"},
        )

    def validate(self, profile: InferenceProfile) -> ProfileValidation:
        runtime = self.identity()
        if profile.runtime_id != self.runtime_id:
            state, rationale = ValidationState.UNSUPPORTED, "profile names a different runtime"
        elif profile.strategy != "resident":
            state, rationale = ValidationState.UNSUPPORTED, "reference adapter only validates CPU resident strategy"
        elif runtime.capabilities.get("identity_probe") != "ready":
            state = ValidationState.UNKNOWN
            rationale = "llama.cpp identity probe: " + str(runtime.capabilities.get("identity_probe", "unknown"))
        elif not profile.options.get("model_path"):
            state, rationale = ValidationState.UNKNOWN, "profile has no adapter model_path option"
        elif not Path(str(profile.options["model_path"])).is_file():
            state, rationale = ValidationState.UNSUPPORTED, "GGUF model artifact is missing"
        else:
            state = ValidationState.SUPPORTED
            rationale = "CPU resident prerequisites available; model loading and execution remain unverified"
        return ProfileValidation(
            runtime=runtime, profile_id=profile.id, artifact_key=profile.artifact.key,
            resources=profile.participating_resources, strategy=profile.strategy,
            options=profile.options, state=state, depth=ValidationDepth.PREFLIGHT,
            rationale=rationale, provenance="probe",
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        validation = self.validate(request.profile)
        runtime = validation.runtime
        if validation.state is not ValidationState.SUPPORTED:
            return ExecutionResult(
                request_id=request.request_id, runtime=runtime, success=False,
                error_class=FailureClass.RUNTIME, error_detail=validation.rationale,
            )
        started = time.monotonic()
        command = [str(self.binary), "-m", str(request.profile.options["model_path"]),
                   "-p", request.prompt, "-n", str(request.max_tokens), "--temp",
                   str(request.temperature), "--no-warmup", "--log-disable"]
        try:
            run = subprocess.run(command, capture_output=True, text=True,
                                 timeout=request.timeout_seconds, check=False)
        except subprocess.TimeoutExpired:
            return ExecutionResult(
                request_id=request.request_id, runtime=runtime, success=False,
                error_class=FailureClass.TIMEOUT, error_detail="llama.cpp timed out",
                timings=Timing(total_seconds=time.monotonic() - started),
            )
        except OSError as exc:
            return ExecutionResult(
                request_id=request.request_id, runtime=runtime, success=False,
                error_class=FailureClass.RESOURCE,
                error_detail=f"llama.cpp process could not start: {exc}",
                timings=Timing(total_seconds=time.monotonic() - started),
            )
        if run.returncode:
            return ExecutionResult(
                request_id=request.request_id, runtime=runtime, success=False,
                error_class=FailureClass.EXECUTION, error_detail=run.stderr[-1000:],
                raw_exit_code=run.returncode,
                timings=Timing(total_seconds=time.monotonic() - started),
            )
        output = run.stdout.removeprefix(request.prompt).strip()
        return ExecutionResult(
            request_id=request.request_id, runtime=runtime, success=True, output=output,
            timings=Timing(total_seconds=time.monotonic() - started),
        )
