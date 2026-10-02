"""Deterministic in-memory Flash delivery contract; no queue/cloud adapter."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


class LeaseError(ValueError):
    """The operation does not hold the current, unexpired lease fence."""


@dataclass(frozen=True)
class Delivery:
    job_id: str
    contract_id: str
    attempt_id: str
    lease_id: str
    lease_expires_at: float


class FlashDeliveryFixture:
    """One-job executable contract fixture using injected clock and ID factory."""

    def __init__(self, *, job_id: str, contract_id: str, lease_seconds: float,
                 clock: Callable[[], float], id_factory: Callable[[str], str]) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.job_id = job_id
        self.contract_id = contract_id
        self.lease_seconds = lease_seconds
        self.clock = clock
        self.id_factory = id_factory
        self.attempt_number = 0
        self.current: Delivery | None = None
        self.completed_result: object | None = None
        self.completed = False

    def claim(self) -> Delivery | None:
        if self.completed:
            return None
        if self.current is not None and self.clock() < self.current.lease_expires_at:
            return None
        self.attempt_number += 1
        self.current = Delivery(
            job_id=self.job_id,
            contract_id=self.contract_id,
            attempt_id=self.id_factory(f"attempt-{self.attempt_number}"),
            lease_id=self.id_factory(f"lease-{self.attempt_number}"),
            lease_expires_at=self.clock() + self.lease_seconds,
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
        self.current = Delivery(**{**current.__dict__, "lease_expires_at": self.clock() + self.lease_seconds})
        return self.current

    def abandon(self, lease_id: str) -> None:
        self._require_current(lease_id)
        self.current = None

    def complete(self, lease_id: str, result: object) -> object:
        if self.completed:
            if self.current is not None and self.current.lease_id == lease_id and result == self.completed_result:
                return self.completed_result
            raise LeaseError("job is already completed by another or differing result")
        self._require_current(lease_id)
        self.completed_result = result
        self.completed = True
        return result
