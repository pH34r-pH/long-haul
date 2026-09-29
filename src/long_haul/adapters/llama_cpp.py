"""llama.cpp CLI adapter. Its flags stay wholly at this edge."""
from __future__ import annotations

import hashlib
import math
import os
import re
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
_ACCELERATED_STRATEGIES = {
    "gpu-offload",
    "gpu_offload",
    "cpu-gpu-split",
    "cpu_gpu_split",
    "multi-gpu",
    "multi_gpu",
}


def _start_version_probe(binary: Path) -> subprocess.Popen[bytes] | None:
    try:
        return subprocess.Popen(
            [str(binary), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError:
        return None
    except PermissionError as exc:
        raise RuntimeError("denied") from exc
    except OSError as exc:
        raise RuntimeError("unusable") from exc


def _read_version_output(process: subprocess.Popen[bytes], deadline: float) -> tuple[str, bytes]:
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", bytes(output)
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, min(4096, IDENTITY_OUTPUT_BYTES + 1 - len(output)))
                if chunk:
                    output.extend(chunk)
                    if len(output) > IDENTITY_OUTPUT_BYTES:
                        return "output-limit", bytes(output)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout", bytes(output)
                try:
                    code = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    return "timeout", bytes(output)
                return ("complete" if code == 0 else "unusable"), bytes(output)


def _parse_version(output: bytes) -> tuple[str, str | None]:
    try:
        version = output.decode("utf-8").strip()
    except UnicodeDecodeError:
        return "unknown-version", None
    if not version or any(ord(c) < 32 and c not in "\n\r\t" for c in version):
        return "unknown-version", None
    return "ready", version


def _cleanup_probe(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.stdout is not None:
        process.stdout.close()
    process.wait()


def _windows_version_probe(binary: Path, timeout: float) -> tuple[str, str | None]:
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        process = subprocess.Popen(
            [str(binary), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    except FileNotFoundError:
        return "missing", None
    except PermissionError:
        return "denied", None
    except OSError:
        return "unusable", None
    try:
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return "timeout", None
        if len(output) > IDENTITY_OUTPUT_BYTES:
            return "output-limit", None
        if process.returncode != 0:
            return "unusable", None
        return _parse_version(output)
    except OSError:
        return "unusable", None


def _version_probe(binary: Path, timeout: float) -> tuple[str, str | None]:
    """Read bounded combined output from a fixed --version argv."""
    if os.name != "posix":
        return _windows_version_probe(binary, timeout)
    try:
        process = _start_version_probe(binary)
    except RuntimeError as exc:
        return str(exc), None
    if process is None:
        return "missing", None
    try:
        status, output = _read_version_output(process, time.monotonic() + timeout)
        return _parse_version(output) if status == "complete" else (status, None)
    except (OSError, ValueError):
        return "unusable", None
    finally:
        _cleanup_probe(process)


def _parse_timings(stderr: str, total_seconds: float) -> Timing:
    timing = Timing(total_seconds=total_seconds)
    for line in stderr.splitlines():
        lowered = line.lower()
        if "load time" in lowered:
            match = re.search(r"load time\s*=\s*([0-9.]+)\s*ms", line, re.IGNORECASE)
            if match:
                timing.load_seconds = float(match.group(1)) / 1000.0
        elif "prompt eval time" in lowered:
            tokens = re.search(r"/\s*(\d+)\s+tokens?", line, re.IGNORECASE)
            rate = re.search(r"([0-9.]+)\s+tokens per second", line, re.IGNORECASE)
            if tokens:
                timing.prompt_tokens = int(tokens.group(1))
            if rate:
                timing.prefill_tps = float(rate.group(1))
        elif "eval time" in lowered:
            tokens = re.search(r"/\s*(\d+)\s+(?:runs?|tokens?)", line, re.IGNORECASE)
            rate = re.search(r"([0-9.]+)\s+tokens per second", line, re.IGNORECASE)
            if tokens:
                timing.generated_tokens = int(tokens.group(1))
            if rate:
                timing.decode_tps = float(rate.group(1))
    return timing


def _accelerated(profile: InferenceProfile) -> bool:
    return profile.strategy in _ACCELERATED_STRATEGIES


def _gpu_layers(profile: InferenceProfile) -> int:
    value = profile.options.get("gpu_layers", 0)
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _append_cpu_options(command: list[str], options: dict[str, object]) -> None:
    """Append upstream llama.cpp CPU placement controls from an edge profile."""
    for key, flag in (
        ("threads", "--threads"),
        ("threads_batch", "--threads-batch"),
    ):
        value = options.get(key)
        if value is not None and not isinstance(value, bool):
            command.extend([flag, str(value)])

    for key, flag in (
        ("cpu_range", "--cpu-range"),
        ("cpu_range_batch", "--cpu-range-batch"),
    ):
        value = options.get(key)
        if value:
            command.extend([flag, str(value)])

    for key, flag in (
        ("cpu_strict", "--cpu-strict"),
        ("cpu_strict_batch", "--cpu-strict-batch"),
    ):
        value = options.get(key)
        if value is not None:
            command.extend([flag, "1" if bool(value) else "0"])

    if options.get("cpu_moe"):
        command.append("--cpu-moe")
    n_cpu_moe = options.get("n_cpu_moe")
    if n_cpu_moe is not None and not isinstance(n_cpu_moe, bool):
        command.extend(["--n-cpu-moe", str(n_cpu_moe)])


def _append_speculative_options(command: list[str], options: dict[str, object]) -> None:
    """Append upstream speculative-decoding controls without owning the policy."""
    for key, flag in (
        ("spec_type", "--spec-type"),
        ("draft_model_path", "--spec-draft-model"),
        ("draft_device", "--spec-draft-device"),
        ("draft_gpu_layers", "--spec-draft-ngl"),
        ("draft_threads", "--spec-draft-threads"),
        ("draft_threads_batch", "--spec-draft-threads-batch"),
        ("draft_cpu_range", "--spec-draft-cpu-range"),
        ("draft_n_max", "--spec-draft-n-max"),
        ("draft_n_min", "--spec-draft-n-min"),
        ("draft_n_cpu_moe", "--spec-draft-n-cpu-moe"),
    ):
        value = options.get(key)
        if value is not None and not isinstance(value, bool):
            command.extend([flag, str(value)])

    if options.get("draft_cpu_moe"):
        command.append("--spec-draft-cpu-moe")


class LlamaCppAdapter:
    runtime_id = "llama.cpp"

    def __init__(
        self,
        binary: str | Path,
        *,
        identity_timeout_seconds: float = IDENTITY_TIMEOUT_SECONDS,
    ):
        if (
            isinstance(identity_timeout_seconds, bool)
            or not math.isfinite(identity_timeout_seconds)
            or not 0 < identity_timeout_seconds <= IDENTITY_TIMEOUT_SECONDS
        ):
            raise ValueError("identity timeout must be finite, positive and at most five seconds")
        # Path('./llama-cli') drops './'; use an absolute path so the version
        # probe and execution address the selected file rather than search PATH.
        self.binary = Path(binary).absolute()
        self.identity_timeout_seconds = identity_timeout_seconds

    def identity(self) -> RuntimeIdentity:
        def unavailable(status: str) -> RuntimeIdentity:
            return RuntimeIdentity(
                runtime_id=self.runtime_id,
                binary_path=str(self.binary),
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
            if (
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                return unavailable("binary-changed")
        except FileNotFoundError:
            return unavailable("missing")
        except PermissionError:
            return unavailable("denied")
        except OSError:
            return unavailable("unusable")
        return RuntimeIdentity(
            runtime_id=self.runtime_id,
            version=version,
            build_id=digest.hexdigest()[:16],
            binary_path=str(self.binary),
            backends=["cpu"],
            capabilities={"resident": True, "identity_probe": "ready"},
        )

    def validate(self, profile: InferenceProfile) -> ProfileValidation:
        runtime = self.identity()
        if profile.runtime_id != self.runtime_id:
            state, rationale = ValidationState.UNSUPPORTED, "profile names a different runtime"
        elif runtime.capabilities.get("identity_probe") != "ready":
            state = ValidationState.UNKNOWN
            rationale = "llama.cpp identity probe: " + str(
                runtime.capabilities.get("identity_probe", "unknown")
            )
        elif not profile.options.get("model_path"):
            state, rationale = ValidationState.UNKNOWN, "profile has no adapter model_path option"
        elif not Path(str(profile.options["model_path"])).is_file():
            state, rationale = ValidationState.UNSUPPORTED, "GGUF model artifact is missing"
        elif profile.strategy == "resident":
            state = ValidationState.SUPPORTED
            rationale = (
                "CPU resident prerequisites available; model loading and execution remain unverified"
            )
        elif _accelerated(profile):
            if _gpu_layers(profile) <= 0:
                state = ValidationState.UNSUPPORTED
                rationale = "accelerated profile requires a positive gpu_layers option"
            else:
                state = ValidationState.UNKNOWN
                rationale = (
                    "accelerated profile requires measured execution qualification; "
                    "preflight does not infer CUDA support from GPU presence"
                )
        else:
            state, rationale = (
                ValidationState.UNSUPPORTED,
                "reference adapter does not implement the requested inference strategy",
            )
        return ProfileValidation(
            runtime=runtime,
            profile_id=profile.id,
            artifact_key=profile.artifact.key,
            resources=profile.participating_resources,
            strategy=profile.strategy,
            options=profile.options,
            state=state,
            depth=ValidationDepth.PREFLIGHT,
            rationale=rationale,
            provenance="probe",
        )

    def _command(self, request: ExecutionRequest) -> list[str]:
        options = request.profile.options
        command = [
            str(self.binary),
            "-m",
            str(options["model_path"]),
            "-p",
            request.prompt,
            "-n",
            str(request.max_tokens),
            "--temp",
            str(request.temperature),
        ]
        if options.get("no_warmup", True):
            command.append("--no-warmup")
        gpu_layers = _gpu_layers(request.profile)
        if gpu_layers > 0:
            command.extend(["-ngl", str(gpu_layers)])
        split_mode = options.get("split_mode")
        if split_mode:
            command.extend(["--split-mode", str(split_mode)])
        tensor_split = options.get("tensor_split")
        if tensor_split:
            if isinstance(tensor_split, (list, tuple)):
                tensor_split = ",".join(str(value) for value in tensor_split)
            command.extend(["--tensor-split", str(tensor_split)])
        main_gpu = options.get("main_gpu")
        if main_gpu is not None and not isinstance(main_gpu, bool):
            command.extend(["--main-gpu", str(main_gpu)])

        # CPU placement is part of an inference profile because thread count and
        # affinity can materially change performance on multi-CCD hosts.
        _append_cpu_options(command, options)
        _append_speculative_options(command, options)
        return command

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        validation = self.validate(request.profile)
        runtime = validation.runtime
        if validation.state is ValidationState.UNSUPPORTED or (
            validation.state is ValidationState.UNKNOWN and not request.allow_unknown_runtime
        ):
            return ExecutionResult(
                request_id=request.request_id,
                runtime=runtime,
                success=False,
                error_class=FailureClass.RUNTIME,
                error_detail=validation.rationale,
            )
        started = time.monotonic()
        env = os.environ.copy()
        visible_devices = request.profile.options.get("cuda_visible_devices")
        if visible_devices is not None:
            env["CUDA_VISIBLE_DEVICES"] = str(visible_devices)
        try:
            run = subprocess.run(
                self._command(request),
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return ExecutionResult(
                request_id=request.request_id,
                runtime=runtime,
                success=False,
                error_class=FailureClass.TIMEOUT,
                error_detail="llama.cpp timed out",
                timings=Timing(total_seconds=time.monotonic() - started),
            )
        except OSError as exc:
            return ExecutionResult(
                request_id=request.request_id,
                runtime=runtime,
                success=False,
                error_class=FailureClass.RESOURCE,
                error_detail=f"llama.cpp process could not start: {exc}",
                timings=Timing(total_seconds=time.monotonic() - started),
            )
        elapsed = time.monotonic() - started
        timings = _parse_timings(run.stderr, elapsed)
        if run.returncode:
            return ExecutionResult(
                request_id=request.request_id,
                runtime=runtime,
                success=False,
                error_class=FailureClass.EXECUTION,
                error_detail=run.stderr[-1000:],
                raw_exit_code=run.returncode,
                timings=timings,
            )
        if _accelerated(request.profile):
            capabilities = dict(runtime.capabilities)
            capabilities["accelerated_execution"] = "measured"
            runtime = runtime.model_copy(
                update={
                    "backends": sorted(set(runtime.backends) | {"cuda"}),
                    "capabilities": capabilities,
                }
            )
        output = run.stdout.removeprefix(request.prompt).strip()
        return ExecutionResult(
            request_id=request.request_id,
            runtime=runtime,
            success=True,
            output=output,
            timings=timings,
        )
