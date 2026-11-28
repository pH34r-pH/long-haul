"""Artifact-bound correctness pilot for the published Qwen3 CPU comparators.

The scientific source owns cases, acceptance and numerical interpretation. This
module executes one bounded attempt inside a caller-owned container/cgroup; it
does not schedule work, acquire authority, or compare incompatible timing rates.
"""
from __future__ import annotations

import hashlib
import math
import os
import platform
import re
import struct
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..benchmarks import BenchmarkObservation, Workload
from ..models import ExecutionMode, InferenceProfile
from ..runtime import RuntimeIdentity
from ._bounded_process import ProcessLimits, ProcessOutcome, run_process

UPSTREAM = "2d53727ebb7a6e5fedd344298b405de50bdff1bc"
ENGINE_SHA256 = {
    "opus": "c70deed1b650472e2b82410c2853a864793fbe484907d50a4672c35d7610238b",
    "neon": "2e67103d508b40a61dc1bc0e3a4239a4574b1490ccea249ca952db4fc9bce991",
}
ENGINE_BYTES = {"opus": 7331, "neon": 10752}
MODEL_BYTES = 643469504
VOCAB_SIZE = 151936
COMMON_CONTEXT = 1024
TokenId = Annotated[int, Field(strict=True, ge=0, lt=VOCAB_SIZE)]


class TokenCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    prompt_ids: tuple[TokenId, ...] = Field(min_length=1)
    expected_ids: tuple[TokenId, ...]
    max_tokens: int = Field(default=64, strict=True, ge=1, le=1024)

    @model_validator(mode="after")
    def bounded_case(self):
        if "\0" in self.prompt or len(self.prompt.encode("utf-8")) > 16000:
            raise ValueError("prompt exceeds the common raw-engine byte envelope")
        if len(self.prompt_ids) + self.max_tokens >= COMMON_CONTEXT:
            raise ValueError("prompt plus allowance exceeds common context envelope")
        if len(self.expected_ids) > self.max_tokens:
            raise ValueError("expected continuation exceeds generation allowance")
        for token in (*self.prompt_ids, *self.expected_ids):
            if type(token) is not int or not 0 <= token < VOCAB_SIZE:
                raise ValueError("token outside Qwen3 vocabulary")
        return self


class RawEngineSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["opus", "neon"]
    binary: Path
    model: Path
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    threads: int = Field(default=1, strict=True, ge=1, le=40)


class RawAttempt(BaseModel):
    case_id: str
    outcome: str
    runtime: RuntimeIdentity
    requested_prompt_ids: list[int]
    expected_generated_ids: list[int]
    max_tokens: int
    requested_profile: InferenceProfile
    requested_limits: ProcessLimits
    process: ProcessOutcome | None = None
    observed_prompt_ids: list[int] | None = None
    observed_generated_ids: list[int] | None = None
    exact_tokens_match: bool | None = None
    engine_reported_tps: dict[str, float] = Field(default_factory=dict)
    environment: dict = Field(default_factory=dict)
    observation: BenchmarkObservation | None = None
    diagnostic: str | None = None
    timing_scope: str = "process-spawn-to-reaped-exit; includes bootstrap and model load"
    scientific_acceptance: str = "not-assessed"
    performance_eligible: bool = False


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _check_model(spec: RawEngineSpec) -> None:
    if not spec.model.is_file() or spec.model.stat().st_size != MODEL_BYTES:
        raise ValueError("asmbin length differs from the pinned v2 shape")
    with spec.model.open("rb") as stream:
        header = stream.read(128)
    if struct.unpack_from("<4s9I", header) != (
        b"ASML", 2, 1024, 3072, 28, 16, 8, 128, VOCAB_SIZE, 4096
    ):
        raise ValueError("unsupported asmbin model header")
    if struct.unpack_from("<4I", header, 48) != (151645, 151643, 524287, 7):
        raise ValueError("unsupported asmbin tokenizer or quantization flags")
    if struct.unpack_from("<2I", header, 40) != (0x358637bd, 0x49742400):
        raise ValueError("unsupported asmbin norm or RoPE parameters")
    if struct.unpack_from("<6Q", header, 64) != (
        128, 9973952, 175280320, 16720896, 643465408, MODEL_BYTES
    ):
        raise ValueError("asmbin section layout differs from pinned v2 format")
    if _digest(spec.model) != spec.model_sha256:
        raise ValueError("model digest mismatch")


def _flags_for_cpus(cpuinfo: str, cpus: tuple[int, ...]) -> list[set[str]]:
    flags = []
    for block in cpuinfo.split("\n\n"):
        fields = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
        fields = {key.strip(): value.strip() for key, value in fields.items()}
        if fields.get("processor", "").isdigit() and int(fields["processor"]) in cpus:
            flags.append(set(fields.get("flags", "").split()))
    return flags


def _check_cpu_quota(quota: str, threads: int) -> None:
    amount, period = quota.split()
    if int(period) <= 0 or (amount != "max" and int(amount) <= 0):
        raise ValueError("invalid observed cgroup CPU quota")
    if amount != "max" and threads * int(period) > int(amount):
        raise ValueError("requested workers exceed observed cgroup CPU quota")


def _environment(limits: ProcessLimits, threads: int) -> dict:
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise ValueError("raw engines require Linux x86-64")
    allowed = os.sched_getaffinity(0)
    if not set(limits.cpus) <= allowed or threads > len(limits.cpus):
        raise ValueError("requested workers exceed the allowed CPU placement")
    flags = _flags_for_cpus(Path("/proc/cpuinfo").read_text(), limits.cpus)
    if len(flags) != len(limits.cpus) or any(not {"avx2", "fma", "f16c"} <= f for f in flags):
        raise ValueError("AVX2/FMA/F16C unavailable on requested CPUs")
    quota_file = Path("/sys/fs/cgroup/cpu.max")
    quota = quota_file.read_text().strip() if quota_file.exists() else None
    if quota is not None:
        _check_cpu_quota(quota, threads)
    return {"platform": platform.platform(), "allowed_cpus": sorted(allowed),
            "requested_initial_affinity": list(limits.cpus), "cpu_max": quota,
            "limits": limits.model_dump(mode="json"), "containment_owner": "external"}


def _binding(spec, profile):
    runtime_id = "qwen3-raw-hex-" + spec.kind
    expected = {"binary_sha256": ENGINE_SHA256[spec.kind],
                "model_sha256": spec.model_sha256, "threads": spec.threads}
    if (profile.runtime_id != runtime_id or type(profile.options.get("threads")) is not int
            or any(profile.options.get(k) != v for k, v in expected.items())):
        raise ValueError("profile must bind exact raw runtime, binary, model and threads")
    return RuntimeIdentity(runtime_id=runtime_id, version=UPSTREAM,
                           build_id="sha256:" + ENGINE_SHA256[spec.kind],
                           binary_path=str(spec.binary.resolve()), backends=["cpu"])


def _token_line(value: str) -> list[int]:
    pieces = value.strip().split(",")
    if pieces[-1] == "":
        pieces.pop()  # The upstream comma-separated printer may terminate a list.
    if any(not re.fullmatch(r"[0-9]+", piece.strip()) for piece in pieces):
        raise ValueError("invalid raw-engine token vector")
    values = [int(piece) for piece in pieces]
    if any(value >= VOCAB_SIZE for value in values):
        raise ValueError("raw-engine token outside vocabulary")
    return values


def parse_output(stderr: bytes, maximum: int) -> tuple[list[int], list[int], dict[str, float]]:
    """Parse unique token records; retain rates strictly as engine self-report."""
    tokens, rates = {}, {}
    for line in stderr.decode("utf-8", errors="strict").splitlines():
        name, separator, value = line.partition(":")
        name = name.removeprefix("[engine] ").strip()
        if separator and name in {"PROMPT_TOKENS", "GEN_TOKENS"}:
            if name in tokens:
                raise ValueError("duplicate raw-engine token record")
            tokens[name] = _token_line(value)
        elif separator and name in {"prefill", "decode"}:
            phase = name
            match = re.search(r"\(([^ ()]+)\s+tok/s\s*[,)]", value)
            rate = float(match.group(1)) if match else math.nan
            if phase in rates or not math.isfinite(rate) or rate < 0:
                raise ValueError("invalid or duplicate engine timing report")
            rates[phase] = rate
    if set(tokens) != {"PROMPT_TOKENS", "GEN_TOKENS"} or not tokens["PROMPT_TOKENS"]:
        raise ValueError("missing raw-engine token evidence")
    if len(tokens["PROMPT_TOKENS"]) >= COMMON_CONTEXT or len(tokens["GEN_TOKENS"]) > maximum:
        raise ValueError("raw-engine token counts exceed request bounds")
    return tokens["PROMPT_TOKENS"], tokens["GEN_TOKENS"], rates


def _evaluate(attempt: RawAttempt, case: TokenCase) -> None:
    if attempt.process.outcome != "completed":
        attempt.outcome = attempt.process.outcome
        return
    try:
        prompt, generated, rates = parse_output(attempt.process.stderr, case.max_tokens)
    except (ValueError, UnicodeError):
        attempt.outcome, attempt.diagnostic = "invalid_output", "raw token/timing contract failed"
        return
    attempt.observed_prompt_ids, attempt.observed_generated_ids = prompt, generated
    attempt.engine_reported_tps = rates
    attempt.exact_tokens_match = (prompt == list(case.prompt_ids) and generated == list(case.expected_ids))
    attempt.outcome = "completed" if attempt.exact_tokens_match else "correctness_failed"


def run_raw_engine(spec: RawEngineSpec, case: TokenCase, limits: ProcessLimits,
                   profile: InferenceProfile, *, provenance: Literal["measured", "simulated"] = "measured") -> RawAttempt:
    """Run one source-owned case; no performance or general quality acceptance.

    Inputs and executables must be read-only within an already admitted boundary.
    Profile options bind the concrete artifacts and all process limits. The rich
    receipt retains exact IDs and elapsed process time; normalized throughput is
    intentionally absent until the timing definition is independently qualified.
    """
    runtime = _binding(spec, profile)
    attempt = RawAttempt(case_id=case.id, outcome="preflight_rejected", runtime=runtime,
                         requested_prompt_ids=list(case.prompt_ids),
                         expected_generated_ids=list(case.expected_ids), max_tokens=case.max_tokens,
                         requested_profile=profile.model_copy(deep=True), requested_limits=limits)
    try:
        attempt.environment = _environment(limits, spec.threads)
        if not spec.binary.is_file() or spec.binary.stat().st_size != ENGINE_BYTES[spec.kind]:
            raise ValueError("raw-engine length or file type mismatch")
        if _digest(spec.binary) != ENGINE_SHA256[spec.kind]:
            raise ValueError("raw-engine binary digest mismatch")
        _check_model(spec)
    except (OSError, ValueError) as error:
        attempt.diagnostic = str(error)
        return attempt
    command = [str(spec.binary.resolve()), "-m", str(spec.model.resolve()), "-p", case.prompt,
               "-n", str(case.max_tokens), "-t", str(spec.threads), "--print-tokens"]
    attempt.process = run_process(command, limits)
    _evaluate(attempt, case)
    bound = profile.model_copy(deep=True)
    bound.options.update(process_limits=limits.model_dump(mode="json"),
                         timing_scope=attempt.timing_scope, prompt_ids=list(case.prompt_ids),
                         cache_state="uncontrolled-after-digest-verification")
    attempt.observation = BenchmarkObservation(
        profile=bound, runtime=runtime, plan_mode=ExecutionMode.LOCAL,
        resources=bound.participating_resources, provenance=provenance,
        workload=Workload(name=case.id, prompt_tokens=len(case.prompt_ids),
                          output_tokens=case.max_tokens, cold=False),
        error=None if attempt.outcome == "completed" else attempt.outcome,
    )
    return attempt
