"""Pinned real Pi RPC adapter; transport and OS cleanup are executor-owned."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime

from ..work.harness import (
    HarnessAttempt,
    HarnessCleanup,
    HarnessInputEvidence,
    progress_event,
)
from ..work.progress import ProgressMonitor, ProgressPolicy
from ..work.repository_models import DigestReference
from .harness_trace import TraceAdmissionDenied, TraceArgumentsInvalid
from .pi_admission import PiToolAdmission, WorkerPromptAdmission, regular_contents
from .pi_trajectory import PiTrajectoryProjector


@dataclass(frozen=True)
class PiPrompt:
    render: Callable[[datetime], str]
    template: DigestReference
    admission: WorkerPromptAdmission | None = None

    def __call__(self, now):
        return self.render(now)


class PiCodingHarness:
    def __init__(self, normalizer, registry, worker, send, receive, prompt, *, clock=None):
        if normalizer.attempt.harness.harness_id != "pi":
            raise ValueError("real Pi adapter requires exact Pi identity")
        self.identity = normalizer.attempt.harness
        self.projector = PiTrajectoryProjector(normalizer, max_pending=1)
        self.guard = PiToolAdmission(normalizer, registry)
        self.worker, self.send, self.receive, self.prompt = worker, send, receive, prompt
        self.clock = clock or (lambda: datetime.now(UTC))
        # Trusted executor owns the lifetime and consistency of each view.
        self.workspace_view = lambda: nullcontext(worker)
        self._started = False
        self._responses = {}
        self._native_session = None
        self._prompted = False
        self._prompt_acknowledged = False
        maximum = normalizer.contract.budget.max_no_progress_steps or 6
        self.monitor = ProgressMonitor(ProgressPolicy(max_no_progress_steps=maximum))
        self.progress_events = []
        self.inputs = []

    def _command(self, command, **arguments):
        identifier = "long-haul-" + command
        if identifier in self._responses:
            raise TraceArgumentsInvalid("duplicate outstanding control command")
        self._responses[identifier] = command
        self.send({"type": command, "id": identifier, **arguments})

    def start(self, attempt: HarnessAttempt):
        if self._started or attempt != self.projector.state.attempt:
            raise ValueError("real Pi attempt already started or mismatched")
        self._started = True
        self._command("get_state")

    def _response(self, record, now):
        expected = self._responses.pop(record.get("id"), None)
        if expected is None or record.get("command") != expected:
            raise TraceArgumentsInvalid("unmatched or unsuccessful Pi control response")
        if record.get("success") is not True:
            if expected == "prompt":
                self._input("denied", now, prior=self.inputs[0])
            raise TraceArgumentsInvalid("unsuccessful Pi control response")
        if expected == "get_state":
            state = record["data"]
            native = state["sessionId"]
            if not isinstance(native, str) or not 1 <= len(native) <= 128 or state["isStreaming"] is not False:
                raise TraceArgumentsInvalid("malformed native session")
            self._native_session = native  # Never persisted as ordinary trace data.
            self._command("set_auto_retry", enabled=False)
            self._command("set_auto_compaction", enabled=False)
        elif expected == "prompt":
            if record.get("data", {}).get("disposition") != "started":
                self._input("denied", now, prior=self.inputs[0])
                raise TraceArgumentsInvalid("Pi did not start the admitted prompt")
            self._prompt_acknowledged = True
            self._input("acknowledged", now, prior=self.inputs[0])
        if not self._responses and not self._prompted and not self.projector.state.cancel_requested:
            self._begin_prompt(now)

    def _make_input(self, status, now, *, text=None, prior=None):
        attempt = self.projector.state.attempt
        receipt = self.projector.normalizer.admission.authorize(attempt, now)
        fingerprint = prior.prompt_fingerprint if prior else self.projector.normalizer.fingerprint("prompt-content", text) if text is not None else None
        sources = self.guard.admit_prompt(getattr(self.prompt, "admission", None), fingerprint, now) if status != "denied" else ()
        entry = HarnessInputEvidence(
            attempt_id=attempt.attempt_id, session_id=attempt.session_id, contract_digest=attempt.contract_digest,
            context_manifest=attempt.context_manifest, task_digest=attempt.digest,
            template=getattr(self.prompt, "template", None), status=status, timestamp=now, policy_receipt=receipt,
            prompt_fingerprint=fingerprint, source_receipts=sources,
            request=DigestReference(reference="artifact:prompt-request", digest=prior.fingerprint) if prior else None,
        )
        return entry

    def _input(self, status, now, *, prior=None):
        self.inputs.append(self._make_input(status, now, prior=prior))

    def _begin_prompt(self, now):
        try:
            self.guard._exact_contract()
            self.projector.normalizer.admission.authorize(self.projector.state.attempt, now)
            prompt = self.prompt(now)  # Recheck context freshness at delivery.
            if not isinstance(prompt, str) or len(prompt.encode()) > 131072:
                raise TraceArgumentsInvalid("worker prompt exceeds bound")
            entry = self._make_input("requested", now, text=prompt)
            self._command("prompt", message=prompt)
        except (ValueError, TypeError):
            self._input("denied", now)
            raise
        self._prompted = True
        self.inputs.append(entry)

    def _record(self, record, now, tick):
        kind = record["type"]
        if kind == "response":
            self._response(record, now)
            return ()
        if kind == "extension_ui_request":
            if not self._prompt_acknowledged or self._native_session is None:
                raise TraceArgumentsInvalid("native interlock arrived before prompt acknowledgement")
            if self.projector.state.cancel_requested:
                raise TraceAdmissionDenied("cancelled attempt cannot admit another operation")
            return self._confirm(record, now)
        elif (kind.startswith("tool_execution") or kind in {"agent_start", "agent_settled"}) and not self._prompt_acknowledged:
            raise TraceArgumentsInvalid("native execution arrived before admitted prompt acknowledgement")
        if kind in {"tool_execution_start", "tool_execution_end"}:
            with self.workspace_view() as worker:
                return self._workspace_record(record, now, tick, worker)
        return self.projector.feed(record, now, tick)

    def _confirm(self, record, now):
        try:
            with self.workspace_view() as worker:
                self.guard.authorize(record, worker, now)
            self.guard.revalidate_confirmation(self.clock())
        except Exception:
            self.send({"type": "extension_ui_response", "id": record.get("id"), "confirmed": False})
            raise
        self.send({"type": "extension_ui_response", "id": record["id"], "confirmed": True})
        return ()

    def _workspace_record(self, record, now, tick, worker):
        kind = record["type"]
        before = after = execution_admission = None
        if kind == "tool_execution_start":
            self.guard.start(record)
            before = self._workspace_reference(worker)
        elif kind == "tool_execution_end":
            execution_admission = self.guard.end(record, worker, now)
            after = self._workspace_reference(worker)
        return self.projector.feed(record, now, tick, candidate_before=before, candidate_after=after,
                                   execution_admission=execution_admission)

    def _workspace_reference(self, worker):
        from ..work.repository_models import DigestReference, content_digest
        from .harness_trace import _canonical
        files = {path: content_digest(data) for path, data in regular_contents(worker).items()}
        return DigestReference(reference="artifact:worker-observed-tree", digest=content_digest(_canonical(files)))

    def poll(self, now: datetime, monotonic_ns: int):
        if not self._started:
            raise ValueError("Pi adapter has not started")
        if self.projector.state.terminal is not None:
            return ()
        start = len(self.projector.events)
        try:
            record = self.receive()
            if record is None:
                return ()
            self._record(record, now, monotonic_ns)
        except TraceAdmissionDenied:
            self.projector._terminate("policy_denied", now, monotonic_ns)
        except (ValueError, TypeError, KeyError):
            self.projector._terminate("protocol_error", now, monotonic_ns)
        observed = self.projector.events[start:]
        for event in observed:
            if event.phase == "end":
                intervention = progress_event(self.monitor, event)
                if intervention is not None:
                    self.progress_events.append(intervention)
                    if intervention.payload["terminate_attempt"] and self.projector.state.terminal is None:
                        self.projector.interrupt("resource_exhausted", now, monotonic_ns)
        return self.projector.events[start:]

    def cancel(self):
        self.projector.state.cancel()
        self._command("clear_queue")
        self._command("abort")

    def finish(self, cleanup: HarnessCleanup, candidate=None):
        return self.projector.state.finish(self.projector.events, cleanup, candidate, inputs=tuple(self.inputs))
