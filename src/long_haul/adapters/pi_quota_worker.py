"""Experimental Pi executor over a locally fenced, quota-backed workspace.

Only trusted scheduler control may call this API. The caller must retain the
fence through evaluation and recovery; this is not a private-work admission.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from ..work.harness import HarnessCleanup
from ..work.repository_models import DigestReference, content_digest
from ..work.worker_candidate import freeze_stopped_worker
from .harness_trace import _canonical
from .pi_harness import PiPrompt
from .pi_worker import DockerGVisorPiWorker, _prepare_control
from .pi_workspace_cleanup import WorkspaceCleanupRequired, _container, _daemon, _volume
from .pi_workspace_copy import copy_docker_workspace
from .pi_workspace_fence import LocalWorkspaceFence, WorkspaceFenceGrant
from .pi_workspace_holder import (
    HolderProfile,
    _verify_privileges,
    allocate_workspace_holder,
    verify_workspace_holder,
)
from .pi_workspace_seed import build_workspace_seed
from .pi_workspace_seeding import HolderSeeder
from .pi_workspace_view import PausedWorkspaceView, _materialize


class QuotaPiWorker(DockerGVisorPiWorker):
    """Prototype; the default Pi executor is not replaced by this class."""

    def run(self, request):
        raise TypeError("quota Pi worker requires run_bounded and a held workspace fence")

    def recover(self, receipt_path=None):
        raise TypeError("quota Pi worker requires recover_bounded and its exact workspace fence")

    def recover_bounded(self, fence):
        grant = getattr(self, "_quota_grant", None)
        if (not isinstance(fence, LocalWorkspaceFence) or not isinstance(grant, WorkspaceFenceGrant)
                or (fence.root, fence.token, fence.fingerprint)
                != (grant._fence.root, grant._fence.token, grant._fence.fingerprint)):
            raise WorkspaceCleanupRequired()
        marker = fence.recover(self._control)
        grant._cleaned = True
        return marker

    @property
    def lease_releasable(self):
        grant = getattr(self, "_quota_grant", None)
        return grant is None or grant.cleaned

    @staticmethod
    def implementation_reference():
        root = Path(__file__).parent
        names = ("pi_quota_worker.py", "pi_workspace_archive.py", "pi_workspace_cleanup.py", "pi_workspace_copy.py",
                 "pi_workspace_fence.py", "pi_workspace_holder.py", "pi_workspace_intent.py", "pi_workspace_seed.py",
                 "pi_workspace_seeding.py", "pi_workspace_view.py")
        manifest = {name: content_digest((root / name).read_bytes()) for name in names}
        manifest["base"] = DockerGVisorPiWorker.implementation_reference().digest
        return DigestReference(reference="urn:long-haul:pi-quota-worker-prototype", digest=content_digest(_canonical(manifest)))

    def _mount_table(self, candidate):
        return {name: value for name, value in super()._mount_table(candidate).items() if name != "/workspace"}

    def _create_arguments(self, candidate, command=None):
        intent = self._quota_intent
        labels = ["--label=" + key + "=" + value for key, value in intent.labels("worker").items()]
        arguments = [*self._containment_arguments(), "--name=" + intent.worker_name, *labels,
                     "--mount=type=volume,src=" + intent.volume_name + ",dst=/workspace",
                     "--workdir=/workspace", "--entrypoint=/usr/local/bin/node"]
        for destination, (source, writable) in self._mount_table(candidate).items():
            arguments.append("--mount=type=bind,src=" + str(source) + ",dst=" + destination + ("" if writable else ",readonly"))
        return [*arguments, self.environment.image, "/control/launch.mjs"]

    def _verify_mounts(self, mounts, candidate):
        binds = [item for item in mounts if item["Type"] == "bind"]
        actual = {item["Destination"]: (Path(item["Source"]).resolve(), item["RW"]) for item in binds}
        volumes = [item for item in mounts if item["Type"] == "volume"]
        volume, = json.loads(self._control("volume", "inspect", self._quota_intent.volume_name))
        if (actual != self._mount_table(candidate) or len(actual) != len(binds)
                or any(item["Type"] not in {"bind", "volume", "tmpfs"} for item in mounts)
                or any(item["Destination"] not in self._expected_tmpfs() for item in mounts if item["Type"] == "tmpfs")):
            raise ValueError("quota Pi worker has unexpected mounts")
        self._verify_quota_mount(volumes, volume)
        _volume(self._control, self._quota_intent.volume_name, self._quota_intent.labels("volume"),
                self._quota_intent.volume_options)

    def _verify_quota_mount(self, volumes, volume):
        if (len(volumes) != 1 or volumes[0].get("Name") != self._quota_intent.volume_name
                or volumes[0].get("Destination") != "/workspace" or volumes[0].get("Source") != volume["Mountpoint"]
                or volumes[0].get("RW") is not True):
            raise ValueError("quota Pi worker has unexpected volume")

    def _verify_configuration(self, container, candidate):
        info = super()._verify_configuration(container, candidate)
        _verify_privileges(info["HostConfig"])
        host = info["HostConfig"]
        labels = info["Config"].get("Labels") or {}
        if (info["Name"] != "/" + self._quota_intent.worker_name
                or host.get("LogConfig", {}).get("Type") != "none"
                or host.get("RestartPolicy", {}).get("Name") != "no" or host.get("AutoRemove") is not False
                or any(labels.get(key) != value for key, value in self._quota_intent.labels("worker").items())):
            raise ValueError("quota Pi worker identity differs from its intent")
        return info

    def run_bounded(self, request, grant):
        """Run one synthetic/admitted attempt while the caller retains its fence."""
        if not isinstance(grant, WorkspaceFenceGrant):
            raise TypeError("quota Pi worker requires an active workspace fence")
        if not self.lease_releasable:
            raise WorkspaceCleanupRequired()
        root, intent = grant.binding()
        self._quota_grant = grant
        self._execution_deadline = time.monotonic() + min(self.environment.limits.wall_seconds,
                                                          request.normalizer.contract.budget.wall_seconds)
        self._validate_request(request)
        if request.event_store is not None:
            raise ValueError("quota worker requires post-cleanup evidence admission")
        if (intent.attempt_fingerprint != request.normalizer.attempt.fingerprint
                or intent.environment_fingerprint != self.environment.fingerprint):
            raise ValueError("workspace intent does not match the exact worker attempt")
        _daemon(self._control, intent.daemon_id)
        scratch = grant.scratch_root()
        control, projection = scratch / "pi-control", scratch / "pi-projection"
        control.mkdir(mode=0o700)
        projection.mkdir(mode=0o700)
        self._control_root, self._quota_intent = control, intent
        _prepare_control(control, request.normalizer.attempt, request.normalizer.tools, request.synthetic)
        projected = self._project(projection, request.base, request.registry, request.normalizer, datetime.now(UTC))
        contents = {path: data for path, data in projected.items() if data is not None}
        holder_profile = HolderProfile(self.environment.image, self.image_id,
                                       tuple(key + "=" + value for key, value in self.image_environment.items()),
                                       self._verify_runtime)
        holder = allocate_workspace_holder(root, intent.token, intent.fingerprint, self._control, holder_profile, grant)
        seed_digest = content_digest(build_workspace_seed(set(request.registry), contents))

        def admit(digest):
            self._ensure_deadline()
            refreshed = self._authorized_projection(request.base, request.registry, request.normalizer, datetime.now(UTC))
            return digest == seed_digest and refreshed == projected

        HolderSeeder(root, intent.token, intent.fingerprint, self._control, self.docker_path, holder,
                     holder_profile, admit, grant).seed(set(request.registry), contents)
        self._ensure_deadline()
        grant.begin_worker()
        return self._run_quota(request, grant, projection, holder, holder_profile)

    def _ensure_deadline(self):
        if time.monotonic() >= self._execution_deadline:
            raise TimeoutError("quota worker execution deadline exceeded")

    def _view(self, container, projection, holder, holder_profile, scratch):
        def verify():
            self._ensure_deadline()
            self._verify_configuration(container, projection)
            verify_workspace_holder(self._control, self._quota_intent, holder_profile, holder, running=True)
            self._ensure_deadline()
        return PausedWorkspaceView(self._control, self.docker_path, container, holder, self._quota_intent.volume_name,
                                   self._quota_intent.volume_options, verify, scratch)

    def _remove_exact(self, container):
        intent = self._quota_intent
        _daemon(self._control, intent.daemon_id)
        if _container(self._control, intent.worker_name, intent.labels("worker")) != container:
            raise ValueError("quota worker identity changed before removal")
        self._control("rm", "--force", container)
        if _container(self._control, intent.worker_name, intent.labels("worker")) is not None:
            raise ValueError("quota worker removal is unconfirmed")
        _daemon(self._control, intent.daemon_id)

    def _check_stopped(self, container, acknowledged):
        stopped, = json.loads(self._control("inspect", container))
        state = stopped["State"]
        if (stopped["Id"] != container or state.get("Running") is not False or state.get("Paused") is not False
                or state.get("Pid") != 0 or state.get("Status") != "exited" or state.get("ExitCode") != 137
                or state.get("OOMKilled") is not False or state.get("Dead") is not False or state.get("Error")):
            raise ValueError("quota worker process-tree stop is unconfirmed "
                             f"(ack={acknowledged}, running={state.get('Running') is True}, "
                             f"paused={state.get('Paused') is True}, exit_137={state.get('ExitCode') == 137}, "
                             f"pid_zero={state.get('Pid') == 0})")

    def _capture_and_remove(self, view, container, holder, holder_profile):
        self._ensure_deadline()
        view._check(paused=False)
        acknowledged = True
        try:
            self._control("kill", "--signal=KILL", container)
        except (OSError, subprocess.SubprocessError):
            acknowledged = False  # Never retry an ambiguous stop; reconcile exact state.
        self._check_stopped(container, acknowledged)
        self._ensure_deadline()
        verify_workspace_holder(self._control, self._quota_intent, holder_profile, holder, running=True)
        view._check_volume_users()
        files = copy_docker_workspace(self.docker_path, holder, 15)
        self._check_stopped(container, acknowledged)
        view._check_volume_users()
        self._ensure_deadline()
        verify_workspace_holder(self._control, self._quota_intent, holder_profile, holder, running=True)
        stopped_files = copy_docker_workspace(self.docker_path, holder, 15)
        self._check_stopped(container, acknowledged)
        view._check_volume_users()
        self._ensure_deadline()
        if stopped_files != files:
            raise ValueError("quota workspace changed after stopping the worker")
        self._remove_exact(container)
        verify_workspace_holder(self._control, self._quota_intent, holder_profile, holder, running=True)
        self._ensure_deadline()
        return files, acknowledged

    def _run_quota(self, request, grant, projection, holder, holder_profile):
        container = self._control(*self._create_arguments(projection)).strip()
        if len(container) != 64 or any(char not in "0123456789abcdef" for char in container):
            raise ValueError("quota worker identity is invalid")
        self._verify_configuration(container, projection)
        view = self._view(container, projection, holder, holder_profile, grant.scratch_root())
        self._workspace_view = view
        files = None
        stop_acknowledged = None
        try:
            with self._attached_process(container) as process:
                harness = self._drive(process, request.normalizer, request.registry, projection,
                                      PiPrompt(request.prompt, request.prompt_template, request.prompt_admission), request.cancel_after)
                self._classify_worker_exit(container, harness)
                if harness.projector.state.terminal == "settled":
                    files, stop_acknowledged = self._capture_and_remove(view, container, holder, holder_profile)
                else:
                    self._remove_exact(container)
        finally:
            del self._workspace_view
        return self._quota_result(request, grant, harness, files, container, stop_acknowledged)

    def _quota_result(self, request, grant, harness, files, container, stop_acknowledged):
        normalizer = request.normalizer
        cleanup_reference = ("artifact:quota-worker-stop-reconciled" if stop_acknowledged is False
                             else "artifact:quota-worker-process-cleanup")
        cleanup = HarnessCleanup(attempt_id=normalizer.attempt.attempt_id, session_id=normalizer.attempt.session_id,
            boundary="sandbox", process_tree_stopped=True, evidence=DigestReference(
                reference=cleanup_reference, digest=content_digest(_canonical({
                    "container": normalizer.fingerprint("quota-container", container), "removed": True,
                    "stop_acknowledged": stop_acknowledged,
                    "environment": self.environment.fingerprint, "implementation": self.identity.digest,
                    "attempt": normalizer.attempt.fingerprint,
                }))))
        snapshot = None
        if files is not None:
            with tempfile.TemporaryDirectory(prefix="worker-final-", dir=grant.scratch_root()) as temporary:
                root = Path(temporary)
                _materialize(root, files)
                self._validate_candidate(request, root, harness.guard)
                snapshot = freeze_stopped_worker(request.source_root, request.base, root, set(request.registry),
                                                 normalizer.contract, scratch_root=grant.scratch_root())
        candidate = DigestReference(reference="artifact:frozen-worker-candidate", digest=snapshot.candidate.fingerprint) if snapshot else None
        result = harness.finish(cleanup, candidate)
        self.last_derived_bindings = dict(harness.guard.derived)
        self.last_native_session_tag = normalizer.fingerprint("pi-native-session", harness._native_session)
        return result, harness.projector.events, snapshot
