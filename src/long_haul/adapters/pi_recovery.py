"""Private recovery receipt for an executor-owned worker that has not stopped."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field

from ..work.harness import HarnessAttempt, HarnessCleanup
from ..work.repository_models import DigestReference, FrozenProjection, content_digest
from ..work.repository_sandbox import confirm_container_absent
from .pi_workspace_intent import _open_private_root

MAX_RECOVERY_BYTES = 262144


class PiWorkerRecovery(FrozenProjection):
    attempt: HarnessAttempt
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace: Path = Field(repr=False)
    control: Path = Field(repr=False)
    environment: DigestReference
    state: Literal["cleanup-required"] = "cleanup-required"
    lease_releasable: Literal[False] = False
    workspace_reusable: Literal[False] = False


class PiWorkerCleanupRequired(RuntimeError):
    def __init__(self, receipt_path: Path | None):
        super().__init__("worker removal is unconfirmed; retain recovery receipt and hold lease")
        self.receipt_path = receipt_path  # Private caller recovery, never ordinary logs.


def retain_recovery(receipt: PiWorkerRecovery):
    root = Path(tempfile.mkdtemp(prefix="long-haul-pi-recovery-"))
    path = root / "recovery.json"
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        output.write(receipt.model_dump_json())
    return path


def _read_recovery_receipt(path: Path) -> bytes:
    """Read bounded bytes; POSIX additionally checks private file and directory metadata.

    The non-POSIX fallback does not qualify equivalent filesystem isolation.
    """
    if os.name != "posix":
        if path.is_symlink() or not path.is_file():
            raise ValueError("recovery receipt is not a regular file")
        with path.open("rb") as source:
            data = source.read(MAX_RECOVERY_BYTES + 1)
    else:
        directory = _open_private_root(path.parent)
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, "rb") as source:
                metadata = os.fstat(source.fileno())
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                        or metadata.st_nlink != 1 or metadata.st_mode & 0o077 or metadata.st_size > MAX_RECOVERY_BYTES):
                    raise ValueError("recovery receipt is not private and bounded")
                data = source.read(MAX_RECOVERY_BYTES + 1)
        finally:
            os.close(directory)
    if len(data) > MAX_RECOVERY_BYTES:
        raise ValueError("recovery receipt exceeds size limit")
    return data


def recover_worker(executor, receipt_path: Path, expected: PiWorkerRecovery) -> HarnessCleanup:
    """Recover only against a separately trusted expected record.

    The caller must authenticate and retain that record independently; the
    on-disk receipt cannot authorize its own container or deletion paths.
    """
    if not isinstance(expected, PiWorkerRecovery):
        raise TypeError("recovery requires an independently trusted expected record")
    receipt_bytes = _read_recovery_receipt(receipt_path)
    receipt = PiWorkerRecovery.model_validate_json(receipt_bytes)
    if receipt != expected:
        raise ValueError("recovery receipt differs from trusted expected record")
    if receipt.environment.digest != executor.environment.fingerprint:
        raise ValueError("recovery requires the exact worker environment")
    executor._control("rm", "--force", "--volumes", receipt.container_id)
    confirm_container_absent(executor._control, receipt.container_id)
    evidence = {"recovery": content_digest(receipt_bytes), "container": receipt.container_id,
                "removed": True, "environment": executor.environment.fingerprint}
    cleanup = HarnessCleanup(
        attempt_id=receipt.attempt.attempt_id, session_id=receipt.attempt.session_id,
        boundary="sandbox", process_tree_stopped=True,
        evidence=DigestReference(reference="artifact:pi-worker-recovered", digest=content_digest(
            json.dumps(evidence, sort_keys=True).encode(),
        )),
    )
    receipt.control.chmod(0o700)
    shutil.rmtree(receipt.workspace)
    shutil.rmtree(receipt.control)
    # Keep immutable failed-state receipt and append the observed cleanup proof.
    (receipt_path.parent / "cleanup.json").write_text(cleanup.model_dump_json())
    executor.recovery_path = None
    return cleanup
