"""Ordered Pi RPC projection without retaining conversations or raw tool payloads.

This is an observer, not an authorization interlock. The execution adapter must
enforce the same allowlist before tool execution and stop on protocol failure.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..work.harness import HarnessState, SemanticActionEventV1, harness_time
from ..work.progress import ProgressKind, StepOutcome
from .harness_trace import (
    TraceAdmissionDenied,
    TraceArgumentsInvalid,
    TraceNormalizer,
    _canonical,
)


@dataclass(frozen=True)
class _Pending:
    event: SemanticActionEventV1
    monotonic_ns: int


class PiTrajectoryProjector:
    def __init__(self, normalizer: TraceNormalizer, *, max_records: int = 2048, max_pending: int = 32):
        if not 1 <= max_records <= 16384 or not 1 <= max_pending <= 64:
            raise ValueError("invalid bounded trace limits")
        self.normalizer = normalizer
        self.state = HarnessState(normalizer.attempt)
        self.max_records, self.max_pending = max_records, max_pending
        self._events: list[SemanticActionEventV1] = []
        self._pending: dict[str, _Pending] = {}
        self._seen: set[str] = set()
        self._wire_sequence = 0
        self._last_tick = 0
        self._last_time: datetime | None = None
        self._failure = None

    @property
    def events(self) -> tuple[SemanticActionEventV1, ...]:
        return tuple(self._events)

    def _append(self, **values):
        attempt = self.state.attempt
        event = SemanticActionEventV1(
            attempt_id=attempt.attempt_id, contract_id=attempt.contract_id, session_id=attempt.session_id,
            sequence=len(self._events) + 1, wire_sequence=self._wire_sequence,
            harness_digest=attempt.harness.fingerprint, normalization=self.normalizer.identity, **values,
        )
        self._events.append(event)
        return event

    def _control(self, summary, now, outcome=StepOutcome.UNKNOWN):
        from ..work.harness import NormalizedArguments

        receipt = self.normalizer.admission.authorize(self.state.attempt, now)
        self._append(
            phase="lifecycle", native_id_mode="none", tool="unknown",
            action="unknown" if summary == "malformed_record" else "recovery",
            timestamp=now, outcome=outcome,
            progress=ProgressKind.NO_PROGRESS if summary == "malformed_record" else ProgressKind.RECOVERY,
            arguments=NormalizedArguments(argument_fingerprint=self.normalizer.fingerprint("lifecycle", summary)),
            summary=summary, consequence="unknown", policy_receipt=receipt,
        )

    def _start(self, record, now, tick, candidate_before):
        native_id, native_tool = record["toolCallId"], record["toolName"]
        if native_id in self._seen or len(self._pending) >= self.max_pending:
            raise TraceArgumentsInvalid("duplicate or excessive outstanding tool calls")
        tool, action, arguments, receipt = self.normalizer.normalize(native_tool, record["args"], now)
        public_native_id, native_id_mode = self.normalizer.native_id(native_id)
        event = self._append(
            native_id=public_native_id, native_id_mode=native_id_mode,
            phase="start", tool=tool, action=action, timestamp=now,
            outcome=StepOutcome.UNKNOWN, progress=ProgressKind.NO_PROGRESS, arguments=arguments,
            summary="unknown_tool" if tool == "unknown" else "started",
            consequence="candidate_mutation" if action in {"edit", "write"} else "read_only" if action != "unknown" else "unknown",
            policy_receipt=receipt, candidate_before=candidate_before,
        )
        self._pending[native_id] = _Pending(event, tick)
        self._seen.add(native_id)
        if tool == "unknown":
            self._terminate("protocol_error", now, tick)

    def _end(self, record, now, tick, candidate_after, execution_admission):
        native_id = record["toolCallId"]
        pending = self._pending.get(native_id)
        if pending is None or record["toolName"] != pending.event.tool or type(record["isError"]) is not bool:
            raise TraceArgumentsInvalid("unmatched or malformed tool completion")
        receipt = self.normalizer.admission.authorize(self.state.attempt, now)
        result = self.normalizer.result(record["result"])
        outcome = StepOutcome.FAILURE if record["isError"] else StepOutcome.SUCCESS
        progress = self._progress(pending.event, outcome, candidate_after)
        self._events.append(pending.event.model_copy(update={
            "sequence": len(self._events) + 1, "wire_sequence": self._wire_sequence,
            "phase": "end", "timestamp": now, "duration_seconds": (tick - pending.monotonic_ns) / 1e9,
            "outcome": outcome, "progress": progress, "policy_receipt": receipt,
            "result_fingerprint": result, "summary": "failed" if record["isError"] else "completed",
            "candidate_after": candidate_after,
            "execution_admission": execution_admission,
        }))
        del self._pending[native_id]

    @staticmethod
    def _progress(start, outcome, candidate_after):
        if outcome is StepOutcome.FAILURE:
            return ProgressKind.NO_PROGRESS
        if start.action in {"read", "search"}:
            return ProgressKind.INFORMATION
        if start.candidate_before and candidate_after and start.candidate_before != candidate_after:
            return ProgressKind.STATE_CHANGE
        return ProgressKind.NO_PROGRESS

    def _flush_pending(self, now, tick):
        if not self._pending:
            return True
        try:
            receipt = self.normalizer.admission.authorize(self.state.attempt, now)
        except TraceAdmissionDenied:
            # The old start record remains immutable history. No copied arguments,
            # disclosed identifiers, or stale policy receipt enter a new annotation.
            self._pending.clear()
            return False
        for pending in self._pending.values():
            self._events.append(pending.event.model_copy(update={
                "sequence": len(self._events) + 1, "wire_sequence": self._wire_sequence,
                "phase": "end", "timestamp": now, "duration_seconds": (tick - pending.monotonic_ns) / 1e9,
                "summary": "interrupted", "outcome": StepOutcome.UNKNOWN, "progress": ProgressKind.NO_PROGRESS,
                "policy_receipt": receipt,
            }))
        self._pending.clear()
        return True

    def _terminate(self, status, now, tick):
        if not self._flush_pending(now, tick):
            status = "policy_denied"
        self.state.settle(status)

    def _retry(self, record, now, tick):
        kind = record["type"]
        if kind == "auto_retry_start":
            self._control("retry_started", now)
        else:
            succeeded = record.get("success")
            if type(succeeded) is not bool:
                raise TraceArgumentsInvalid("retry status is not explicit")
            self._failure = None if succeeded else "provider_failure"
            self._control("retry_completed", now, StepOutcome.SUCCESS if succeeded else StepOutcome.FAILURE)
    def _compaction(self, record, now, tick):
        if record["type"] == "compaction_start":
            self._control("compaction_started", now)
        elif record.get("aborted") or "errorMessage" in record:
            self._failure = "context_exhausted" if record.get("reason") == "overflow" else "provider_failure"
            self._control("compaction_failed", now, StepOutcome.FAILURE)

    def _message_end(self, record, now, tick):
        message = record.get("message", {})
        if not isinstance(message, dict):
            raise TraceArgumentsInvalid("malformed message envelope")
        if message.get("role") == "assistant" and message.get("stopReason") == "error":
            self._failure = "provider_failure"

    def _settled(self, record, now, tick):
        if type(record.get("aborted")) is not bool:
            raise TraceArgumentsInvalid("settled status is not explicit")
        status = "cancelled" if record["aborted"] else self._failure or "settled"
        if self._pending and status == "settled":
            status = "protocol_error"
        if not self._flush_pending(now, tick):
            raise TraceAdmissionDenied("pending action annotation is no longer authorized")
        self._control("settled", now)
        self.state.settle(status)

    def _lifecycle(self, record, now, tick):
        handlers = {"auto_retry_start": self._retry, "auto_retry_end": self._retry,
                    "compaction_start": self._compaction, "compaction_end": self._compaction,
                    "message_end": self._message_end, "agent_settled": self._settled}
        handler = handlers.get(record["type"])
        if handler is not None:
            handler(record, now, tick)

    def _clock(self, now, tick):
        now = harness_time(now)
        if (type(tick) is not int or tick < self._last_tick
                or self._last_time is not None and now < self._last_time):
            raise ValueError("harness observer clock moved backwards or is invalid")
        self._last_tick, self._last_time = tick, now
        return now

    def feed(self, record: dict, now: datetime, monotonic_ns: int, *,
             candidate_before=None, candidate_after=None, execution_admission=None) -> tuple[SemanticActionEventV1, ...]:
        now = self._clock(now, monotonic_ns)
        if self.state.terminal is not None or self.state.finished:
            raise ValueError("cannot append to a terminal attempt")
        start = len(self._events)
        self._wire_sequence += 1
        if self._wire_sequence > self.max_records:
            self._terminate("resource_exhausted", now, monotonic_ns)
            return self.events[start:]
        try:
            if not isinstance(record, dict) or len(_canonical(record)) > 1048576:
                raise TraceArgumentsInvalid("invalid bounded protocol record")
            kind = record["type"]
            if kind == "tool_execution_start":
                self._start(record, now, monotonic_ns, candidate_before)
            elif kind == "tool_execution_end":
                self._end(record, now, monotonic_ns, candidate_after, execution_admission)
            else:
                self._lifecycle(record, now, monotonic_ns)
        except TraceAdmissionDenied:
            self._terminate("policy_denied", now, monotonic_ns)
        except (ValueError, TypeError, KeyError):
            try:
                self._control("malformed_record", now)
            except TraceAdmissionDenied:
                pass
            self._terminate("protocol_error", now, monotonic_ns)
        return self.events[start:]

    def eof(self, now: datetime, monotonic_ns: int) -> None:
        if self.state.terminal is None:
            self.interrupt("harness_crash", now, monotonic_ns)

    def interrupt(self, status, now: datetime, monotonic_ns: int) -> None:
        """Trusted executor reports its deadline, process exit, or cancellation."""
        if status not in {"cancelled", "timed_out", "harness_crash", "resource_exhausted"}:
            raise ValueError("unsupported external interruption")
        if self.state.terminal is not None:
            raise ValueError("attempt is already terminal")
        self._terminate(status, self._clock(now, monotonic_ns), monotonic_ns)
