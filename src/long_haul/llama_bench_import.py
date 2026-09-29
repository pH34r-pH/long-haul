"""Import native llama-bench JSON into Long Haul benchmark evidence."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from .benchmark_matrix import MatrixManifest, _profile, load_manifest
from .benchmarks import BenchmarkObservation, BenchmarkStore, Workload
from .models import ExecutionMode
from .runtime import ProfileValidation, RuntimeIdentity, ValidationDepth, ValidationState


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


def import_profile(
    manifest_path: str | Path,
    profile_id: str,
    raw_path: str | Path,
    output_dir: str | Path,
) -> dict[str, object]:
    manifest: MatrixManifest = load_manifest(manifest_path)
    specification = next(
        (item for item in manifest.profiles if item.id == profile_id),
        None,
    )
    if specification is None:
        raise ValueError(f"profile is not declared in matrix: {profile_id}")

    raw = json.loads(Path(raw_path).read_text())
    if not isinstance(raw, list) or not raw:
        raise ValueError("llama-bench output must be a non-empty JSON array")
    rows = [LlamaBenchRow.model_validate(item) for item in raw]
    model_path = rows[0].model_filename
    if any(row.model_filename != model_path for row in rows):
        raise ValueError("one import may contain only one model artifact path")

    profile = _profile(specification, manifest.artifact, model_path)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    store = BenchmarkStore(out / "benchmarks.jsonl")
    observation_ids = []
    for row in rows:
        workload = _workload(row)
        observation = BenchmarkObservation(
            profile=profile,
            plan_mode=ExecutionMode.LOCAL,
            resources=profile.participating_resources,
            workload=workload,
            provenance="measured",
            prefill_tps=row.avg_ts if row.n_prompt and not row.n_gen else None,
            decode_tps=row.avg_ts if row.n_gen else None,
            utilization={
                "llama_bench_stddev_tps": row.stddev_ts
                if row.stddev_ts is not None
                else 0.0
            },
        )
        store.append(observation)
        observation_ids.append(observation.id)

    runtime = _runtime(rows[0])
    validation = ProfileValidation(
        runtime=runtime,
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
    report_path = out / "matrix-report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text())
        if report.get("schema") != "long-haul-benchmark-matrix/v1":
            raise ValueError("existing matrix report has an unsupported schema")
    else:
        report = {
            "schema": "long-haul-benchmark-matrix/v1",
            "binary": "native-llama-bench",
            "model": model_path,
            "manifest": manifest.model_dump(mode="json"),
            "profiles": [],
            "benchmark_store": str(out / "benchmarks.jsonl"),
        }

    record = {
        "profile": profile.model_dump(mode="json"),
        "preflight": validation.model_dump(mode="json"),
        "runs": [
            {"observation_id": observation_id}
            for observation_id in observation_ids
        ],
        "measured_validation": validation.model_dump(mode="json"),
    }
    report["profiles"] = [
        item
        for item in report.get("profiles", [])
        if item.get("profile", {}).get("id") != profile.id
    ] + [record]
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return record
