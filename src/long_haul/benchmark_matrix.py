"""Manifest-driven bounded benchmark matrix for a single physical vessel."""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field

from .adapters.llama_cpp import LlamaCppAdapter
from .benchmarks import BenchmarkObservation, BenchmarkStore, Workload
from .models import ExecutionMode, InferenceProfile, ModelArtifact
from .runtime import ProfileValidation, ValidationDepth, ValidationState, ExecutionRequest


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


def load_manifest(path: str | Path) -> MatrixManifest:
    data = yaml.safe_load(Path(path).read_text())
    return MatrixManifest.model_validate(data)


def run(
    binary: str,
    model: str,
    manifest_path: str,
    output_dir: str,
    timeout_seconds: float = 900,
) -> dict[str, object]:
    manifest = load_manifest(manifest_path)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    store = BenchmarkStore(out / "benchmarks.jsonl")
    adapter = LlamaCppAdapter(binary)
    profile_results: list[dict[str, object]] = []

    for specification in manifest.profiles:
        options = dict(specification.options)
        options["model_path"] = model
        profile = InferenceProfile(
            id=specification.id,
            runtime_id="llama.cpp",
            strategy=specification.strategy,
            artifact=manifest.artifact,
            participating_resources=specification.resources,
            options=options,
        )
        validation = adapter.validate(profile)
        profile_record: dict[str, object] = {
            "profile": profile.model_dump(mode="json"),
            "preflight": validation.model_dump(mode="json"),
            "runs": [],
        }
        runnable = validation.state is ValidationState.SUPPORTED or (
            validation.state is ValidationState.UNKNOWN and specification.explore_unknown
        )
        if not runnable:
            profile_results.append(profile_record)
            continue

        measured_validation: ProfileValidation | None = None
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
                observation = BenchmarkObservation(
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
                    error=None if result.success else (
                        f"{result.error_class.value if result.error_class else 'FAIL'}: "
                        f"{result.error_detail or 'execution failed'}"
                    ),
                )
                store.append(observation)
                profile_record["runs"].append(
                    {
                        "workload": workload.name,
                        "repetition": repetition + 1,
                        "observation_id": observation.id,
                        "result": result.model_dump(mode="json"),
                    }
                )
                if result.success:
                    measured_validation = ProfileValidation(
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
                elif validation.state is ValidationState.UNKNOWN:
                    break

        if measured_validation is not None:
            profile_record["measured_validation"] = measured_validation.model_dump(mode="json")
        profile_results.append(profile_record)

    report: dict[str, object] = {
        "schema": "long-haul-benchmark-matrix/v1",
        "binary": str(Path(binary)),
        "model": str(Path(model)),
        "manifest": manifest.model_dump(mode="json"),
        "profiles": profile_results,
        "benchmark_store": str(out / "benchmarks.jsonl"),
    }
    (out / "matrix-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report
