"""Model-neutral progress classification, repetition detection, and bounded recovery."""
from __future__ import annotations

import hashlib
import json
from enum import Enum

from pydantic import BaseModel, Field


class ProgressKind(str, Enum):
    INFORMATION = "information_acquisition"
    STATE_CHANGE = "state_change"
    VERIFICATION = "verification"
    RECOVERY = "recovery"
    NO_PROGRESS = "no_progress"


class StepOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class AttemptStep(BaseModel):
    step_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    arguments: dict[str, object] = Field(default_factory=dict)
    outcome: StepOutcome = StepOutcome.UNKNOWN
    progress: ProgressKind
    result_fingerprint: str | None = None


class ProgressPolicy(BaseModel):
    recent_window: int = Field(default=8, ge=1)
    repeated_call_limit: int = Field(default=3, ge=2)
    consecutive_limit: int = Field(default=3, ge=2)
    repeated_failure_limit: int = Field(default=2, ge=2)
    max_no_progress_steps: int = Field(default=6, ge=1)


class ProgressIntervention(BaseModel):
    reason: str
    signature: str
    recent_count: int
    consecutive_count: int
    no_progress_steps: int
    terminate_attempt: bool = False


class ProgressState(BaseModel):
    signatures: list[str] = Field(default_factory=list)
    last_signature: str | None = None
    consecutive_count: int = 0
    no_progress_steps: int = 0
    failure_pairs: list[str] = Field(default_factory=list)
    interventions: int = 0


def stable_signature(action: str, arguments: dict[str, object]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{action}:{canonical}".encode()).hexdigest()


def failure_pair_signature(step: AttemptStep) -> str | None:
    if step.outcome is not StepOutcome.FAILURE:
        return None
    return f"{stable_signature(step.action, step.arguments)}:{step.result_fingerprint or 'unknown'}"


class ProgressMonitor:
    def __init__(self, policy: ProgressPolicy | None = None):
        self.policy = policy or ProgressPolicy()
        self.state = ProgressState()

    def observe(self, step: AttemptStep) -> ProgressIntervention | None:
        signature = stable_signature(step.action, step.arguments)
        recent = self.state.signatures[-self.policy.recent_window :]
        recent_count = recent.count(signature) + 1
        consecutive = self.state.consecutive_count + 1 if signature == self.state.last_signature else 1

        self.state.signatures.append(signature)
        self.state.signatures = self.state.signatures[-self.policy.recent_window :]
        self.state.last_signature = signature
        self.state.consecutive_count = consecutive

        if step.progress in {ProgressKind.STATE_CHANGE, ProgressKind.RECOVERY}:
            self.state.no_progress_steps = 0
        elif step.progress is ProgressKind.VERIFICATION and step.outcome is StepOutcome.SUCCESS:
            # Re-verification is legitimate evidence and should not be treated as an idle loop.
            self.state.no_progress_steps = 0
        else:
            self.state.no_progress_steps += 1

        failure_signature = failure_pair_signature(step)
        repeated_failures = 0
        if failure_signature:
            self.state.failure_pairs.append(failure_signature)
            self.state.failure_pairs = self.state.failure_pairs[-self.policy.recent_window :]
            repeated_failures = self.state.failure_pairs.count(failure_signature)

        reason: str | None = None
        if repeated_failures >= self.policy.repeated_failure_limit:
            reason = "repeated_failure_pair"
        elif consecutive >= self.policy.consecutive_limit:
            reason = "consecutive_repeated_action"
        elif recent_count >= self.policy.repeated_call_limit and step.progress is not ProgressKind.VERIFICATION:
            reason = "repeated_action"
        elif self.state.no_progress_steps >= self.policy.max_no_progress_steps:
            reason = "no_progress_budget_exhausted"

        if reason is None:
            return None
        self.state.interventions += 1
        return ProgressIntervention(
            reason=reason,
            signature=signature,
            recent_count=recent_count,
            consecutive_count=consecutive,
            no_progress_steps=self.state.no_progress_steps,
            terminate_attempt=self.state.no_progress_steps >= self.policy.max_no_progress_steps,
        )

    def recovery_event_payload(self, contract_id: str, attempt_id: str, intervention: ProgressIntervention) -> dict[str, object]:
        return {
            "contract_id": contract_id,
            "attempt_id": attempt_id,
            "reason": intervention.reason,
            "signature": intervention.signature,
            "recent_count": intervention.recent_count,
            "consecutive_count": intervention.consecutive_count,
            "no_progress_steps": intervention.no_progress_steps,
            "terminate_attempt": intervention.terminate_attempt,
            "intervention_count": self.state.interventions,
        }
