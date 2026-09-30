"""Import native llama-bench JSON into Long Haul benchmark evidence."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from .benchmark_matrix import MatrixManifest, _profile, load_manifest
from .benchmarks import BenchmarkObservation, BenchmarkStore, Workload
from .models import ExecutionMode
from .runtime import (
    ProfileValidation,
    RuntimeIdentity,
    ValidationDepth,
    ValidationState,
)


class LlamaBenchRow(BaseModel):
    build_commit: str
    build_number: int | str
    backends: str = ""
    model_filename: str
    n_gpu_layers: int
    split_mode: str
    main_gpu: int = 0
    tensor_split: str = "0.00"
    n_prompt: int = 0
    n_gen: int = 0
    n_depth: int = 0
    avg_ts: float
    stddev_ts: float | None = None


def _workload(row: LlamaBenchRow) -> Workload:
    suffix = f"@d{row.n_depth}" if row.n_depth else ""
    if row.n_prompt and row.n_gen:
        name = f"pg{row.n_prompt},{row.n_gen}{suffix}"
    elif row.n_prompt:
        name = f"pp{row.n_prompt}{suffix}"
    else:
        name = f"tg{row.n_gen}{suffix}"
    return Workload(
        name=name,
        prompt_tokens=row.n_prompt,
        output_tokens=row.n_gen,
        cold=False,
    )


def _runtime(row: LlamaBenchRow) -> RuntimeIdentity:
    backends = [part.strip().lower() for part in row.backends.split(",") if part.strip()]
    return RuntimeIdentity(
        runtime_id="llama.cpp",
        version=str(row.build_number),
        build_id=row.build_commit,
        backends=backends,
        capabilities={"llama_bench": True},
    )


def _rows(path: str | Path) -> list[LlamaBenchRow]:
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, list) or not raw:
        raise ValueError("llama-bench output must be a non-empty JSON array")
    rows = [LlamaBenchRow.model_validate(item) for item in raw]
    model_path = rows[0].model_filename
    if any(row.model_filename != model_path for row in rows):
        raise ValueError("one import may contain only one model artifact path")
    return rows


def _append_observations(
    store: BenchmarkStore,
    profile,
    rows: list[LlamaBenchRow],
) -> list[str]:
    observation_ids = []
    for row in rows:
        observation = BenchmarkObservation(
            runtime=_runtime(row),
            profile=profile,
            plan_mode=ExecutionMode.LOCAL,
            resources=profile.participating_resources,
            workload=_workload(row),
            provenance="measured",
            prefill_tps=row.avg_ts if row.n_prompt and not row.n_gen else None,
            decode_tps=row.avg_ts if row.n_gen else None,
            utilization={
                "llama_bench_stddev_tps": row.stddev_ts
                if row.stddev_ts is not None
                else 0.0
            },
        )
        observation_ids.append(store.append(observation).id)
    return observation_ids


def _validation(profile, row: LlamaBenchRow) -> ProfileValidation:
    return ProfileValidation(
        runtime=_runtime(row),
        profile_id=profile.id,
        artifact_key=profile.artifact.key,
        resources=profile.participating_resources,
        strategy=profile.strategy,
        options=profile.options,
        state=ValidationState.SUPPORTED,
        depth=ValidationDepth.BENCHMARK,
        rationale="native llama-bench completed for the declared execution profile",
        provenance="measured",
    )


def _report(
    path: Path,
    manifest: MatrixManifest,
    model_path: str,
    benchmark_path: Path,
) -> dict[str, object]:
    if path.exists():
        report = json.loads(path.read_text())
        if report.get("schema") != "long-haul-benchmark-matrix/v1":
            raise ValueError("existing matrix report has an unsupported schema")
        return report
    return {
        "schema": "long-haul-benchmark-matrix/v1",
        "binary": "native-llama-bench",
        "model": model_path,
        "manifest": manifest.model_dump(mode="json"),
        "profiles": [],
        "benchmark_store": str(benchmark_path),
    }


def _record(profile, validation: ProfileValidation, observation_ids: list[str]) -> dict[str, object]:
    measured = validation.model_dump(mode="json")
    return {
        "profile": profile.model_dump(mode="json"),
        "preflight": measured,
        "runs": [{"observation_id": item} for item in observation_ids],
        "measured_validation": measured,
    }


def import_profile(
    manifest_path: str | Path,
    profile_id: str,
    raw_path: str | Path,
    output_dir: str | Path,
) -> dict[str, object]:
    manifest = load_manifest(manifest_path)
    specification = next((item for item in manifest.profiles if item.id == profile_id), None)
    if specification is None:
        raise ValueError(f"profile is not declared in matrix: {profile_id}")

    rows = _rows(raw_path)
    profile = _profile(specification, manifest.artifact, rows[0].model_filename)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    benchmark_path = out / "benchmarks.jsonl"
    observation_ids = _append_observations(BenchmarkStore(benchmark_path), profile, rows)
    validation = _validation(profile, rows[0])
    record = _record(profile, validation, observation_ids)

    report_path = out / "matrix-report.json"
    report = _report(report_path, manifest, rows[0].model_filename, benchmark_path)
    report["profiles"] = [
        item
        for item in report.get("profiles", [])
        if item.get("profile", {}).get("id") != profile.id
    ] + [record]
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return record
