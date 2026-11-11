"""Private recovery receipt for an executor-owned worker that has not stopped."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field

from ..work.harness import HarnessAttempt, HarnessCleanup
from ..work.repository_models import DigestReference, FrozenProjection, content_digest


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


def recover_worker(executor, receipt_path: Path) -> HarnessCleanup:
    """Explicit retry; no result, candidate or reuse right exists until rm succeeds."""
    receipt = PiWorkerRecovery.model_validate_json(receipt_path.read_bytes())
    if receipt.environment.digest != executor.environment.fingerprint:
        raise ValueError("recovery requires the exact worker environment")
    executor._control("rm", "--force", "--volumes", receipt.container_id)
    evidence = {"recovery": content_digest(receipt_path.read_bytes()), "container": receipt.container_id,
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
