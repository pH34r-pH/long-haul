"""Manifest-driven bounded benchmark matrix for a single physical vessel."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field

from .adapters.llama_bench import LlamaBenchAdapter, LlamaBenchSweep
from .adapters.llama_cpp import LlamaCppAdapter
from .benchmarks import BenchmarkObservation, BenchmarkStore, Workload
from .models import ExecutionMode, InferenceProfile, ModelArtifact
from .runtime import (
    ExecutionRequest,
    ProfileValidation,
    ValidationDepth,
    ValidationState,
)


class MatrixProfile(BaseModel):
    id: str
    strategy: str
    resources: list[str] = Field(min_length=1)
    options: dict[str, object] = Field(default_factory=dict)
    explore_unknown: bool = False


class MatrixWorkload(BaseModel):
    name: str
    prompt: str
    max_tokens: int = Field(default=32, ge=1, le=4096)
    temperature: float = Field(default=0, ge=0)
    repetitions: int = Field(default=1, ge=1, le=100)


class MatrixManifest(BaseModel):
    artifact: ModelArtifact
    profiles: list[MatrixProfile] = Field(min_length=1)
    workloads: list[MatrixWorkload] = Field(min_length=1)
    performance: LlamaBenchSweep | None = None


def load_manifest(path: str | Path) -> MatrixManifest:
    data = yaml.safe_load(Path(path).read_text())
    return MatrixManifest.model_validate(data)


def _profile(specification: MatrixProfile, artifact: ModelArtifact, model: str) -> InferenceProfile:
    options = dict(specification.options)
    options["model_path"] = model
    return InferenceProfile(
        id=specification.id,
        runtime_id="llama.cpp",
        strategy=specification.strategy,
        artifact=artifact,
        participating_resources=specification.resources,
        options=options,
    )


def _observation(
    profile: InferenceProfile,
    workload: MatrixWorkload,
    result,
) -> BenchmarkObservation:
    error = None
    if not result.success:
        label = result.error_class.value if result.error_class else "FAIL"
        error = f"{label}: {result.error_detail or 'execution failed'}"
    return BenchmarkObservation(
        profile=profile,
        plan_mode=ExecutionMode.LOCAL,
        resources=profile.participating_resources,
        workload=Workload(
            name=workload.name,
            prompt_tokens=result.timings.prompt_tokens or 0,
            output_tokens=result.timings.generated_tokens or 0,
            cold=True,
        ),
        provenance="measured",
        load_seconds=result.timings.load_seconds,
        ttft_seconds=result.timings.ttft_seconds,
        prefill_tps=result.timings.prefill_tps,
        decode_tps=result.timings.decode_tps,
        error=error,
    )


def _measured_validation(profile: InferenceProfile, result) -> ProfileValidation:
    return ProfileValidation(
        runtime=result.runtime,
        profile_id=profile.id,
        artifact_key=profile.artifact.key,
        resources=profile.participating_resources,
        strategy=profile.strategy,
        options=profile.options,
        state=ValidationState.SUPPORTED,
        depth=ValidationDepth.EXECUTION,
        rationale="profile completed measured benchmark execution",
        provenance="measured",
    )


def _run_native_benchmark(
    adapter: LlamaBenchAdapter,
    store: BenchmarkStore,
    profile: InferenceProfile,
    model: str,
    sweep: LlamaBenchSweep,
    timeout_seconds: float,
) -> dict[str, object]:
    result = adapter.run(profile, model, sweep, timeout_seconds)
    observation_ids = [store.append(item).id for item in result.observations]
    return {
        "success": result.success,
        "observation_ids": observation_ids,
        "error": result.error,
        "exit_code": result.exit_code,
    }


def _run_profile(
    adapter: LlamaCppAdapter,
    store: BenchmarkStore,
    manifest: MatrixManifest,
    specification: MatrixProfile,
    model: str,
    timeout_seconds: float,
    bench_adapter: LlamaBenchAdapter | None = None,
) -> dict[str, object]:
    profile = _profile(specification, manifest.artifact, model)
    validation = adapter.validate(profile)
    record: dict[str, object] = {
        "profile": profile.model_dump(mode="json"),
        "preflight": validation.model_dump(mode="json"),
        "runs": [],
    }
    runnable = validation.state is ValidationState.SUPPORTED or (
        validation.state is ValidationState.UNKNOWN and specification.explore_unknown
    )
    if not runnable:
        return record

    measured: ProfileValidation | None = None
    for workload in manifest.workloads:
        for repetition in range(workload.repetitions):
            request = ExecutionRequest(
                request_id=str(uuid4()),
                profile=profile,
                prompt=workload.prompt,
                max_tokens=workload.max_tokens,
                temperature=workload.temperature,
                timeout_seconds=timeout_seconds,
                allow_unknown_runtime=specification.explore_unknown,
            )
            result = adapter.execute(request)
            observation = store.append(_observation(profile, workload, result))
            record["runs"].append(
                {
                    "workload": workload.name,
                    "repetition": repetition + 1,
                    "observation_id": observation.id,
                    "result": result.model_dump(mode="json"),
                }
            )
            if result.success:
                measured = _measured_validation(profile, result)
            elif validation.state is ValidationState.UNKNOWN:
                break

    if measured is not None:
        record["measured_validation"] = measured.model_dump(mode="json")
        if bench_adapter is not None and manifest.performance is not None:
            record["native_benchmark"] = _run_native_benchmark(
                bench_adapter,
                store,
                profile,
                model,
                manifest.performance,
                timeout_seconds,
            )
    return record


def run(
    binary: str,
    model: str,
    manifest_path: str,
    output_dir: str,
    timeout_seconds: float = 900,
    bench_binary: str | None = None,
    profile_ids: set[str] | None = None,
) -> dict[str, object]:
    manifest = load_manifest(manifest_path)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    store = BenchmarkStore(out / "benchmarks.jsonl")
    adapter = LlamaCppAdapter(binary)
    bench_adapter = LlamaBenchAdapter(bench_binary) if bench_binary else None
    selected = [
        spec for spec in manifest.profiles
        if profile_ids is None or spec.id in profile_ids
    ]
    missing = (profile_ids or set()) - {spec.id for spec in selected}
    if missing:
        raise ValueError(f"unknown benchmark profiles: {', '.join(sorted(missing))}")
    profiles = [
        _run_profile(
            adapter,
            store,
            manifest,
            spec,
            model,
            timeout_seconds,
            bench_adapter,
        )
        for spec in selected
    ]
    report: dict[str, object] = {
        "schema": "long-haul-benchmark-matrix/v1",
        "binary": str(Path(binary)),
        "model": str(Path(model)),
        "manifest": manifest.model_dump(mode="json"),
        "profiles": profiles,
        "benchmark_store": str(out / "benchmarks.jsonl"),
    }
    (out / "matrix-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report
