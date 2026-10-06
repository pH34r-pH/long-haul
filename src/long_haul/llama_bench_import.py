"""Import native llama-bench JSON into Long Haul benchmark evidence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from .benchmark_matrix import MatrixManifest, _profile, load_manifest
from .benchmarks import (
    BenchmarkCorrelation,
    BenchmarkImportProvenance,
    BenchmarkObservation,
    BenchmarkStore,
    Workload,
    imported_observation_id,
    source_occurrence_id,
)
from .models import ExecutionMode, InferenceProfile
from .runtime import (
    ProfileValidation,
    RuntimeIdentity,
    ValidationDepth,
    ValidationState,
)


@dataclass(frozen=True, slots=True)
class LlamaBenchImportOptions:
    """Caller-supplied identity and correlation metadata for one offline import."""

    runtime_identity: RuntimeIdentity | None = None
    measured_at: datetime | None = None
    source_id: str | None = None
    source_repository: str | None = None
    source_commit: str | None = None
    importer_commit: str | None = None
    correlation: BenchmarkCorrelation | None = None


@dataclass(frozen=True, slots=True)
class _ImportContext:
    profile: InferenceProfile
    options: LlamaBenchImportOptions
    source_id: str
    source_sha256: str
    imported_at: datetime


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
    avg_ts: float = Field(gt=0, allow_inf_nan=False)
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
    rows: list[LlamaBenchRow],
    context: _ImportContext,
) -> list[BenchmarkObservation]:
    observations = []
    options = context.options
    for occurrence_index, row in enumerate(rows):
        occurrence_id = source_occurrence_id(
            context.source_id, context.source_sha256, occurrence_index
        )
        import_provenance = BenchmarkImportProvenance(
            source_id=context.source_id,
            source_sha256=context.source_sha256,
            occurrence_index=occurrence_index,
            source_occurrence_id=occurrence_id,
            source_repository=options.source_repository,
            source_commit=options.source_commit,
            importer_commit=options.importer_commit,
            imported_at=context.imported_at,
            measured_at=options.measured_at,
        )
        observation = BenchmarkObservation(
            id=imported_observation_id(occurrence_id),
            timestamp=options.measured_at or context.imported_at,
            runtime=options.runtime_identity or _runtime(row),
            profile=context.profile,
            plan_mode=ExecutionMode.LOCAL,
            resources=context.profile.participating_resources,
            workload=_workload(row),
            provenance="measured" if options.measured_at is not None else "imported",
            prefill_tps=row.avg_ts if row.n_prompt and not row.n_gen else None,
            decode_tps=row.avg_ts if row.n_gen else None,
            utilization={
                "llama_bench_stddev_tps": row.stddev_ts
                if row.stddev_ts is not None
                else 0.0
            },
            correlation=options.correlation,
            import_provenance=import_provenance,
        )
        observations.append(store.append_imported(observation))
    return observations


def _validation(
    profile,
    row: LlamaBenchRow,
    runtime_identity=None,
    measured_at=None,
    imported_at=None,
) -> ProfileValidation:
    return ProfileValidation(
        runtime=runtime_identity or _runtime(row),
        timestamp=measured_at or imported_at or datetime.now(UTC),
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


def _record(
    profile,
    validation: ProfileValidation,
    observations: list[BenchmarkObservation],
) -> dict[str, object]:
    measured = validation.model_dump(mode="json")
    return {
        "profile": profile.model_dump(mode="json"),
        "preflight": measured,
        "runs": [
            {
                "observation_id": observation.id,
                **(
                    {
                        "source_occurrence_id": observation.import_provenance.source_occurrence_id
                    }
                    if observation.import_provenance is not None
                    else {}
                ),
            }
            for observation in observations
        ],
        "measured_validation": measured,
    }


def import_profile(
    manifest_path: str | Path,
    profile_id: str,
    raw_path: str | Path,
    output_dir: str | Path,
    *,
    options: LlamaBenchImportOptions | None = None,
) -> dict[str, object]:
    selected_options = options or LlamaBenchImportOptions()
    if selected_options.measured_at is not None and selected_options.measured_at.tzinfo is None:
        raise ValueError("measured_at must be timezone-aware")
    manifest = load_manifest(manifest_path)
    specification = next((item for item in manifest.profiles if item.id == profile_id), None)
    if specification is None:
        raise ValueError(f"profile is not declared in matrix: {profile_id}")

    rows = _rows(raw_path)
    profile = _profile(specification, manifest.artifact, rows[0].model_filename)
    raw_bytes = Path(raw_path).read_bytes()
    source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    basename_digest = hashlib.sha256(Path(raw_path).name.encode("utf-8")).hexdigest()
    default_source_id = "file-" + hashlib.sha256(
        f"{basename_digest}:{source_sha256}".encode("ascii")
    ).hexdigest()[:32]
    import_source_id = (
        selected_options.source_id
        if selected_options.source_id is not None
        else default_source_id
    )
    imported_at = datetime.now(UTC)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    benchmark_path = out / "benchmarks.jsonl"
    context = _ImportContext(
        profile,
        selected_options,
        import_source_id,
        source_sha256,
        imported_at,
    )
    observations = _append_observations(
        BenchmarkStore(benchmark_path),
        rows,
        context,
    )
    validation = _validation(
        profile,
        rows[0],
        selected_options.runtime_identity,
        selected_options.measured_at,
        observations[0].import_provenance.imported_at
        if observations[0].import_provenance is not None
        else imported_at,
    )
    record = _record(profile, validation, observations)

    report_path = out / "matrix-report.json"
    report = _report(report_path, manifest, rows[0].model_filename, benchmark_path)
    report["profiles"] = [
        item
        for item in report.get("profiles", [])
        if item.get("profile", {}).get("id") != profile.id
    ] + [record]
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return record
