"""Deterministic scripted transport for the shared adapter conformance suite.

No subprocess, sandbox, model, tool execution, or physical result is claimed.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime

from ..work.harness import HarnessAttempt, HarnessCleanup, HarnessResult
from ..work.repository_models import DigestReference
from .harness_trace import TraceNormalizer
from .pi_trajectory import PiTrajectoryProjector


class FixtureCodingHarness:
    def __init__(self, normalizer: TraceNormalizer, records: tuple[dict, ...]):
        if normalizer.attempt.harness.harness_id != "fixture":
            raise ValueError("scripted fixture cannot assert real harness qualification")
        self.identity = normalizer.attempt.harness
        self.projector = PiTrajectoryProjector(normalizer)
        self._records = deque(records)
        self._started = False

    def start(self, attempt: HarnessAttempt) -> None:
        if self._started or attempt != self.projector.state.attempt:
            raise ValueError("fixture attempt is already started or mismatched")
        self._started = True

    def poll(self, now: datetime, monotonic_ns: int):
        if not self._started:
            raise ValueError("fixture attempt has not started")
        if self.projector.state.terminal is not None:
            return ()
        if self.projector.state.cancel_requested:
            return self.projector.feed({"type": "agent_settled", "aborted": True}, now, monotonic_ns)
        if self._records:
            return self.projector.feed(self._records.popleft(), now, monotonic_ns)
        self.projector.eof(now, monotonic_ns)
        return ()

    def cancel(self) -> None:
        self.projector.state.cancel()

    def finish(self, cleanup: HarnessCleanup, candidate: DigestReference | None = None) -> HarnessResult:
        return self.projector.state.finish(self.projector.events, cleanup, candidate)
