"""Replaceable sandbox seam using maintained Docker/OCI plus pinned gVisor."""
from __future__ import annotations

import hashlib
import json
import os
import selectors
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .repository_evaluation_models import (
    CandidateCommand,
    EvaluatorEnvironment,
    SandboxExecutionReceipt,
)
from .repository_models import DigestReference, content_digest

START_MARKER = b"LONG_HAUL_SUT_STARTED_V1\n"
PYTHON_BOOTSTRAP = (
    "import hashlib,os,runpy,sys; path=sys.argv[1]; expected=sys.argv[2]; "
    "assert hashlib.sha256(open(path,'rb').read()).hexdigest()==expected; "
    "os.write(2,b'LONG_HAUL_SUT_STARTED_V1\\n'); assert os.read(0,1)==b'\\0'; "
    "sys.argv=[path,*sys.argv[3:]]; "
    "runpy.run_path(path,run_name='__main__')"
)


@dataclass(frozen=True)
class SandboxResult:
    outcome: str
    stdout: bytes = field(default=b"", repr=False)
    stderr: bytes = field(default=b"", repr=False)
    exit_code: int | None = None
    receipt: DigestReference | None = None
    execution_receipt: SandboxExecutionReceipt | None = None
    diagnostic: str | None = None
    outputs_observed: bool = True
    startup_verified: bool = False


class SandboxAdapter(Protocol):
    environment: EvaluatorEnvironment
    identity: DigestReference

    def execute(self, candidate: Path, command: CandidateCommand) -> SandboxResult: ...


EVALUATOR_CANDIDATE_PREFIX = "long-haul-evaluator-"


class SandboxCleanupRequired(RuntimeError):
    """Private recovery information; do not release the candidate or lease."""

    def __init__(self, container_id: str, workspace: Path):
        super().__init__("sandbox cleanup is unconfirmed; retain the workspace and lease")
        self.container_id = container_id
        self.workspace = workspace
        self.lease_releasable = False
        self.workspace_mode_restricted = None


def confirm_container_absent(control, container_id):
    """Require a point-in-time Docker census after acknowledged removal."""
    if len(container_id) != 64 or any(char not in "0123456789abcdef" for char in container_id):
        raise ValueError("invalid sandbox container identity")
    output = control("container", "ls", "--all", "--no-trunc", "--filter", "id=" + container_id,
                     "--format", "{{.ID}}")
    if output.strip():
        raise ValueError("sandbox container removal is unconfirmed")


def _control_environment() -> dict[str, str]:
    # Do not forward credentials, caller environment, proxies, or Docker endpoints.
    return {"PATH": os.defpath, "LANG": "C.UTF-8"}


def _bounded_output(process, stdin, limits, *, deadline=None):
    deadline = deadline if deadline is not None else time.monotonic() + limits.wall_seconds
    stdout, stderr = bytearray(), bytearray()
    with selectors.DefaultSelector() as selector:
        for stream, target in ((process.stdout, stdout), (process.stderr, stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, target)
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, memoryview(stdin))
        while selector.get_map():
            if time.monotonic() >= deadline:
                return "timeout", bytes(stdout), bytes(stderr)
            for key, _ in selector.select(timeout=min(0.1, max(0, deadline - time.monotonic()))):
                if key.events == selectors.EVENT_WRITE:
                    if not key.data:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        process.stdin = None
                    else:
                        try:
                            written = os.write(key.fileobj.fileno(), key.data)
                            selector.modify(key.fileobj, selectors.EVENT_WRITE, key.data[written:])
                        except BrokenPipeError:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                            process.stdin = None
                    continue
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    key.data.extend(chunk)
                    if len(stdout) + len(stderr) > limits.max_output_bytes:
                        retained_stdout = bytes(stdout[:limits.max_output_bytes])
                        retained_stderr = bytes(stderr[:limits.max_output_bytes - len(retained_stdout)])
                        return "output_limit", retained_stdout, retained_stderr
    process.wait(timeout=max(0.01, deadline - time.monotonic()))
    return "completed", bytes(stdout), bytes(stderr)


def _startup_receipt(process, deadline):
    received = bytearray()
    os.set_blocking(process.stderr.fileno(), False)
    with selectors.DefaultSelector() as selector:
        selector.register(process.stderr, selectors.EVENT_READ)
        while len(received) < len(START_MARKER) and time.monotonic() < deadline:
            for key, _ in selector.select(timeout=min(0.1, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fileobj.fileno(), len(START_MARKER) - len(received))
                if not chunk:
                    return bytes(received)
                received.extend(chunk)
    return bytes(received)


class DockerGVisorSandbox:
    """Trusted executor owns Docker; candidate never receives its socket or secrets."""

    def __init__(self, environment: EvaluatorEnvironment, runsc_path: Path, docker_path=Path("/usr/bin/docker")):
        self.environment, self.runsc_path, self.docker_path = environment, runsc_path, docker_path
        self.identity = self.implementation_reference()
        self._validate_profile()
        if environment.limits.pids < 128 or environment.limits.memory_mib < 256:
            raise ValueError("Docker/gVisor requires at least 128 host tasks and 256 MiB")
        self._verify_runtime()

    def _validate_profile(self):
        environment = self.environment
        if (environment.runtime, environment.runtime_entrypoint, environment.toolchain) != (
                "long-haul-gvisor", "runsc", "isolated-python3"):
            raise ValueError("unsupported Docker/gVisor execution profile")

    def _verify_runtime(self):
        environment, runsc_path = self.environment, self.runsc_path
        if content_digest(runsc_path.read_bytes()) != environment.runtime_binary_digest:
            raise ValueError("gVisor runtime binary fingerprint mismatch")
        for name, expected in environment.runtime_files:
            with (runsc_path.parent / name).open("rb") as stream:
                actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != expected:
                raise ValueError("gVisor tool bundle fingerprint mismatch")
        runtime = self._control("info", "--format", "{{json .Runtimes}}")
        runtimes = json.loads(runtime)
        actual = runtimes.get(environment.runtime, {})
        if (Path(actual.get("path", "")).resolve() != runsc_path.resolve()
                or actual.get("runtimeArgs") != ["--network=none"]):
            raise ValueError("required gVisor runtime/network configuration is unavailable")
        image = json.loads(self._control("image", "inspect", environment.image))[0]
        self.image_id = image["Id"]  # Acquisition is a separate trusted step.
        self.image_environment = dict(value.split("=", 1) for value in image["Config"].get("Env", []))

    @staticmethod
    def implementation_reference() -> DigestReference:
        return DigestReference(reference="urn:long-haul:docker-gvisor-v1", digest=content_digest(Path(__file__).read_bytes()))

    def _control(self, *arguments):
        with tempfile.TemporaryDirectory(prefix="long-haul-docker-config-") as config:
            result = subprocess.run([str(self.docker_path), "--config", config, *arguments], env=_control_environment(),
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=True)
        if len(result.stdout) > 1048576:
            raise ValueError("sandbox control output exceeds limit")
        return result.stdout.decode()

    def _create_arguments(self, candidate, command):
        program_digest = hashlib.sha256((candidate / command.program).read_bytes()).hexdigest()
        return [*self._containment_arguments(),
                "--mount=type=bind,src=" + str(candidate) + ",dst=/candidate,readonly",
                "--workdir=/candidate", "--env=PYTHONDONTWRITEBYTECODE=1", "--env=PYTHONHASHSEED=0",
                self.environment.image, "/usr/local/bin/python3", "-I", "-S", "-c", PYTHON_BOOTSTRAP,
                "/candidate/" + command.program, program_digest, *command.arguments]

    def _containment_arguments(self):
        limits = self.environment.limits
        arguments = ["create", "--interactive", "--attach=stdin", "--attach=stdout", "--attach=stderr",
                "--pull=never", "--platform=linux/amd64", "--init",
                "--runtime=" + self.environment.runtime, "--network=none", "--read-only",
                "--user=65534:65534", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--cpus=" + str(limits.cpus), "--memory=" + str(limits.memory_mib) + "m",
                "--memory-swap=" + str(limits.memory_mib) + "m", "--pids-limit=" + str(limits.pids),
                "--log-driver=none"]
        arguments.extend("--tmpfs=" + path + ":" + options for path, options in self._expected_tmpfs().items())
        return arguments

    def _verify_configuration(self, container, candidate):
        info = json.loads(self._control("inspect", container))[0]
        host = info["HostConfig"]
        mounts = info["Mounts"]
        limits = self.environment.limits
        actual = (host["Runtime"], host["NetworkMode"], host["ReadonlyRootfs"], info["Config"]["User"],
                  host["Memory"], host["MemorySwap"], host["PidsLimit"], host["NanoCpus"], info["Image"],
                  "ALL" in [cap.upper() for cap in host.get("CapDrop", [])],
                  host.get("SecurityOpt") in (["no-new-privileges"], ["no-new-privileges:true"]),
                  host.get("Tmpfs", {}), host.get("Privileged", False), host.get("PidMode", ""),
                  info["Config"]["AttachStdout"], info["Config"]["AttachStderr"], info["Config"]["OpenStdin"])
        expected = (self.environment.runtime, "none", True, "65534:65534", limits.memory_mib * 1048576,
                    limits.memory_mib * 1048576, limits.pids, int(limits.cpus * 1000000000), self.image_id,
                    True, True, self._expected_tmpfs(), False, "", True, True, True)
        if (actual != expected or host.get("CapAdd")
                or any(host.get(key) == "host" for key in ("IpcMode", "UTSMode", "UsernsMode", "CgroupnsMode"))):
            raise ValueError("candidate containment configuration mismatch")
        self._verify_environment(info["Config"].get("Env", []))
        self._verify_mounts(mounts, candidate)
        return info

    def _expected_tmpfs(self):
        return {"/tmp": "rw,nosuid,nodev,noexec,size=" + str(self.environment.limits.scratch_mib) + "m,nr_inodes=1024"}

    def _verify_environment(self, observed_environment):
        environment = dict(self.image_environment)
        environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
        actual_environment = dict(value.split("=", 1) for value in observed_environment)
        if actual_environment != environment or len(actual_environment) != len(observed_environment):
            raise ValueError("candidate environment differs from pinned image and explicit profile")

    @staticmethod
    def _verify_mounts(mounts, candidate):
        binds = [mount for mount in mounts if mount["Type"] == "bind"]
        if (len(binds) != 1 or binds[0]["Destination"] != "/candidate" or binds[0]["RW"]
                or Path(binds[0]["Source"]).resolve() != candidate.resolve()
                or any(m["Type"] not in {"bind", "tmpfs"} for m in mounts)
                or any(m["Destination"] != "/tmp" for m in mounts if m["Type"] == "tmpfs")):
            raise ValueError("candidate containment configuration mismatch")

    def assert_ready(self):
        pending = getattr(self, "_pending_cleanup", None)
        if pending is not None:
            raise SandboxCleanupRequired(*pending)

    def execute(self, candidate, command):
        self.assert_ready()
        container = None
        result = SandboxResult("invalid", diagnostic="sandbox_invalid", outputs_observed=False)
        removed = False
        try:
            candidate_id = self._control(*self._create_arguments(candidate, command)).strip()
            if len(candidate_id) != 64 or any(c not in "0123456789abcdef" for c in candidate_id):
                raise ValueError("malformed sandbox container identity")
            container = candidate_id
            self._verify_configuration(container, candidate)
            result = self._execute(container, command)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            pass
        finally:
            if container is not None:
                try:
                    self._control("rm", "--force", "--volumes", container)
                    confirm_container_absent(self._control, container)
                    removed = True
                except BaseException:  # noqa: BLE001 -- interruptions also leave cleanup unconfirmed.
                    self._pending_cleanup = (container, Path(candidate))
                    raise SandboxCleanupRequired(*self._pending_cleanup) from None
        return self._retained_result(container, result, removed)

    @contextmanager
    def _attached_process(self, container):
        with tempfile.TemporaryDirectory(prefix="long-haul-docker-config-") as config:
            process = subprocess.Popen([str(self.docker_path), "--config", config, "start", "--attach", "--interactive", container],
                                       env=_control_environment(), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                yield process
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def _execute(self, container, command):
        with self._attached_process(container) as process:
            deadline = time.monotonic() + self.environment.limits.wall_seconds
            startup = _startup_receipt(process, deadline)
            if startup != START_MARKER:
                return SandboxResult("invalid", stderr=startup, diagnostic="startup_unverified")
            outcome, stdout, stderr = _bounded_output(
                process, b"\0" + command.stdin.encode(), self.environment.limits, deadline=deadline,
            )
            if outcome != "completed":
                try:
                    self._control("kill", container)
                except subprocess.CalledProcessError:
                    pass  # It may have exited between the output cap and kill.
            state = json.loads(self._control("inspect", container))[0]["State"]
            if state.get("OOMKilled"):
                outcome = "resource_exhausted"
            if state.get("Running") or state.get("Error"):
                outcome = "invalid"
            return SandboxResult(outcome, stdout, stderr, state.get("ExitCode"), startup_verified=True)

    def _retained_result(self, container, result, removed):
        if container is None:
            return result
        outcome = result.outcome if removed else "invalid"
        diagnostic = result.diagnostic if removed else "cleanup_failed"
        facts = SandboxExecutionReceipt(
            container_id=container, environment_fingerprint=self.environment.fingerprint,
            resolved_image_id=self.image_id if result.outputs_observed else None,
            outcome=outcome, cleanup="removed" if removed else "failed",
            process_stopped=True if removed else None,
            startup_verified=result.startup_verified,
            stdout_digest=content_digest(result.stdout) if result.outputs_observed else None,
            stderr_digest=content_digest(result.stderr) if result.outputs_observed else None, exit_code=result.exit_code,
        )
        return SandboxResult(outcome, result.stdout, result.stderr, result.exit_code,
                             DigestReference(reference="urn:long-haul:sandbox:" + container, digest=facts.fingerprint),
                             facts, diagnostic, result.outputs_observed, result.startup_verified)
