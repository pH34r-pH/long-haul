"""Runtime-neutral execution and feasibility contracts."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from .models import InferenceProfile


class ValidationState(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


class FailureClass(str, Enum):
    SYSTEM = "FAIL_SYSTEM"; RUNTIME = "FAIL_RUNTIME"; ARTIFACT = "FAIL_ARTIFACT"
    RESOURCE = "FAIL_RESOURCE"; EXECUTION = "FAIL_EXECUTION"; TIMEOUT = "FAIL_TIMEOUT"


class RuntimeIdentity(BaseModel):
    runtime_id: str
    version: str | None = None
    build_id: str | None = None
    binary_path: str | None = None
    backends: list[str] = Field(default_factory=list)
    capabilities: dict[str, bool | str] = Field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return "|".join([self.runtime_id, self.version or "unknown", self.build_id or "unknown", ",".join(sorted(self.backends))])


class ProfileValidation(BaseModel):
    runtime: RuntimeIdentity
    profile_id: str
    artifact_key: str
    resources: list[str]
    strategy: str
    options: dict[str, object] = Field(default_factory=dict)
    state: ValidationState
    rationale: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provenance: Literal["measured", "probe", "simulated"] = "probe"

    @property
    def fingerprint(self) -> str:
        return "|".join([self.runtime.fingerprint, self.profile_id, self.artifact_key, ",".join(sorted(self.resources)), self.strategy, repr(sorted(self.options.items()))])


class ExecutionRequest(BaseModel):
    request_id: str
    profile: InferenceProfile
    prompt: str
    max_tokens: int = Field(default=32, ge=1, le=4096)
    temperature: float = Field(default=0, ge=0)
    timeout_seconds: float = Field(default=120, gt=0)


class Timing(BaseModel):
    load_seconds: float | None = None; ttft_seconds: float | None = None
    prompt_tokens: int | None = None; generated_tokens: int | None = None
    prefill_tps: float | None = None; decode_tps: float | None = None; total_seconds: float | None = None


class ExecutionResult(BaseModel):
    request_id: str; runtime: RuntimeIdentity
    success: bool; output: str | None = None; timings: Timing = Field(default_factory=Timing)
    error_class: FailureClass | None = None; error_detail: str | None = None
    raw_exit_code: int | None = None


class RuntimeAdapter(Protocol):
    runtime_id: str
    def identity(self) -> RuntimeIdentity: ...
    def validate(self, profile: InferenceProfile) -> ProfileValidation: ...
    def execute(self, request: ExecutionRequest) -> ExecutionResult: ...
