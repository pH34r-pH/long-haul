"""Deterministic in-memory Flash delivery contract; no queue/cloud adapter."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .contracts import WorkContract


class LeaseError(ValueError):
    """The operation does not hold the current, unexpired lease fence."""


class VerificationError(ValueError):
    """A result is malformed or rejected by the trusted external verifier."""


@dataclass(frozen=True)
class Delivery:
    job_id: str
    contract_id: str
    attempt_id: str
    lease_id: str
    lease_expires_at: float


class FlashDeliveryFixture:
    """One-job executable contract fixture using injected clock, verifier, and IDs."""

    def __init__(self, *, job_id: str, contract: WorkContract, lease_seconds: float,
                 clock: Callable[[], float], id_factory: Callable[[str], str],
                 verifier: Callable[[WorkContract, Any], bool]) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.job_id = job_id
        self.contract = contract.model_copy(deep=True)
        self.contract_id = self.contract.contract_id
        self.lease_seconds = lease_seconds
        self.clock = clock
        self.id_factory = id_factory
        self.verifier = verifier
        self.started_at = clock()
        self.attempt_number = 0
        self.current: Delivery | None = None
        self._accepted_result_json: str | None = None
        self.completed = False

    @property
    def completed_result(self) -> Any | None:
        """Return a detached copy; mutation cannot alter accepted completion."""
        if self._accepted_result_json is None:
            return None
        return json.loads(self._accepted_result_json)

    def _deadline(self) -> float:
        return self.started_at + self.contract.budget.wall_seconds

    def claim(self) -> Delivery | None:
        if self.completed or self.clock() >= self._deadline():
            return None
        if self.attempt_number >= self.contract.budget.max_attempts:
            return None
        if self.current is not None and self.clock() < self.current.lease_expires_at:
            return None
        self.attempt_number += 1
        now = self.clock()
        self.current = Delivery(
            job_id=self.job_id,
            contract_id=self.contract_id,
            attempt_id=self.id_factory(f"attempt-{self.attempt_number}"),
            lease_id=self.id_factory(f"lease-{self.attempt_number}"),
            lease_expires_at=min(now + self.lease_seconds, self._deadline()),
        )
        return self.current

    def _require_current(self, lease_id: str) -> Delivery:
        if self.completed:
            raise LeaseError("job is already completed")
        if self.current is None or self.current.lease_id != lease_id:
            raise LeaseError("stale or unknown lease")
        if self.clock() >= self.current.lease_expires_at:
            raise LeaseError("lease expired")
        return self.current

    def renew(self, lease_id: str) -> Delivery:
        current = self._require_current(lease_id)
        self.current = Delivery(**{
            **current.__dict__,
            "lease_expires_at": min(self.clock() + self.lease_seconds, self._deadline()),
        })
        return self.current

    def abandon(self, lease_id: str) -> None:
        self._require_current(lease_id)
        self.current = None

    def complete(self, lease_id: str, result: object) -> object:
        try:
            result_json = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
            normalized_result = json.loads(result_json)
        except (TypeError, ValueError, OverflowError) as exc:
            raise VerificationError("result must be finite JSON data") from exc
        if self.completed:
            if self.current is not None and self.current.lease_id == lease_id and result_json == self._accepted_result_json:
                return self.completed_result
            raise LeaseError("job is already completed by another or differing result")
        self._require_current(lease_id)
        # This trusted verifier represents the independent contract evaluator.
        # Worker result fields cannot assert their own acceptance.
        if not self.verifier(self.contract, normalized_result):
            raise VerificationError("trusted verifier rejected result")
        # Verification may be slow or yield; the lease and contract deadline
        # must still be valid at the acceptance commit point.
        self._require_current(lease_id)
        self._accepted_result_json = result_json
        self.completed = True
        return self.completed_result
