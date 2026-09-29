"""Normalize llama.cpp's native llama-bench output into Long Haul observations."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from ..benchmarks import BenchmarkObservation, Workload
from ..models import ExecutionMode, InferenceProfile


class LlamaBenchSweep(BaseModel):
    prompt_tokens: list[int] = Field(default_factory=lambda: [512])
    generation_tokens: list[int] = Field(default_factory=lambda: [128])
    context_depths: list[int] = Field(default_factory=lambda: [0])
    repetitions: int = Field(default=5, ge=1, le=100)
    threads: list[int] = Field(default_factory=list)
    batch_sizes: list[int] = Field(default_factory=list)
    ubatch_sizes: list[int] = Field(default_factory=list)


class LlamaBenchRun(BaseModel):
    success: bool
    observations: list[BenchmarkObservation] = Field(default_factory=list)
    error: str | None = None
    exit_code: int | None = None


def _values(values: list[int]) -> str:
    return ",".join(str(value) for value in values)


def _gpu_layers(profile: InferenceProfile) -> int:
    if profile.strategy == "resident":
        return 0
    value = profile.options.get("gpu_layers", -1)
    if isinstance(value, bool):
        return -1
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _tensor_split(profile: InferenceProfile) -> str | None:
    value = profile.options.get("tensor_split")
    if not value:
        return None
    if isinstance(value, (list, tuple)):
        return "/".join(str(item) for item in value)
    return str(value).replace(",", "/")


def _command(
    binary: Path,
    model: str,
    profile: InferenceProfile,
    sweep: LlamaBenchSweep,
) -> list[str]:
    command = [
        str(binary),
        "-m",
        model,
        "-o",
        "json",
        "-r",
        str(sweep.repetitions),
        "-p",
        _values(sweep.prompt_tokens),
        "-n",
        _values(sweep.generation_tokens),
        "-d",
        _values(sweep.context_depths),
        "-ngl",
        str(_gpu_layers(profile)),
    ]
    if sweep.threads:
        command.extend(["-t", _values(sweep.threads)])
    if sweep.batch_sizes:
        command.extend(["-b", _values(sweep.batch_sizes)])
    if sweep.ubatch_sizes:
        command.extend(["-ub", _values(sweep.ubatch_sizes)])
    split_mode = profile.options.get("split_mode")
    if split_mode:
        command.extend(["-sm", str(split_mode)])
    tensor_split = _tensor_split(profile)
    if tensor_split:
        command.extend(["-ts", tensor_split])
    main_gpu = profile.options.get("main_gpu")
    if main_gpu is not None and not isinstance(main_gpu, bool):
        command.extend(["-mg", str(main_gpu)])
    return command


_STATS_FIELDS = (
    "build_commit",
    "build_number",
    "cpu_info",
    "gpu_info",
    "backends",
    "model_type",
    "model_size",
    "model_n_params",
    "n_batch",
    "n_ubatch",
    "n_threads",
    "n_gpu_layers",
    "split_mode",
    "main_gpu",
    "devices",
    "tensor_split",
    "test_time",
    "avg_ns",
    "stddev_ns",
    "stddev_ts",
    "samples_ns",
    "samples_ts",
)


def _observation(profile: InferenceProfile, record: dict[str, object], repetitions: int) -> BenchmarkObservation:
    prompt_tokens = int(record.get("n_prompt") or 0)
    output_tokens = int(record.get("n_gen") or 0)
    context_depth = int(record.get("n_depth") or 0)
    throughput = float(record.get("avg_ts") or 0)
    statistics = {key: record[key] for key in _STATS_FIELDS if key in record}
    return BenchmarkObservation(
        profile=profile,
        plan_mode=ExecutionMode.LOCAL,
        resources=profile.participating_resources,
        workload=Workload(
            name="llama-bench",
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            context_depth_tokens=context_depth,
            repetitions=repetitions,
            cold=False,
        ),
        provenance="measured",
        prefill_tps=throughput if prompt_tokens and not output_tokens else None,
        decode_tps=throughput if output_tokens and not prompt_tokens else None,
        statistics=statistics,
    )


class LlamaBenchAdapter:
    """Thin wrapper around llama-bench; Long Haul owns normalization, not the benchmark loop."""

    def __init__(self, binary: str | Path):
        self.binary = Path(binary).absolute()

    def run(
        self,
        profile: InferenceProfile,
        model: str,
        sweep: LlamaBenchSweep,
        timeout_seconds: float = 1800,
    ) -> LlamaBenchRun:
        env = os.environ.copy()
        visible_devices = profile.options.get("cuda_visible_devices")
        if visible_devices is not None:
            env["CUDA_VISIBLE_DEVICES"] = str(visible_devices)
        try:
            completed = subprocess.run(
                _command(self.binary, model, profile, sweep),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return LlamaBenchRun(success=False, error=f"llama-bench could not complete: {exc}")
        if completed.returncode:
            return LlamaBenchRun(
                success=False,
                error=completed.stderr[-2000:] or "llama-bench failed",
                exit_code=completed.returncode,
            )
        try:
            payload = json.loads(completed.stdout)
            records = payload if isinstance(payload, list) else [payload]
            observations = [
                _observation(profile, record, sweep.repetitions)
                for record in records
                if isinstance(record, dict)
            ]
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return LlamaBenchRun(success=False, error=f"invalid llama-bench JSON: {exc}")
        return LlamaBenchRun(success=bool(observations), observations=observations)
