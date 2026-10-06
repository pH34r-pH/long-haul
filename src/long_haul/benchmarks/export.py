"""Offline, versioned benchmark fact export for private derived indexes."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import ExecutionMode, InferenceProfile
from ..runtime import RuntimeIdentity
from .store import (
    BenchmarkCorrelation,
    BenchmarkImportProvenance,
    BenchmarkObservation,
    BenchmarkStore,
    Workload,
    imported_observation_id,
)

FACT_SCHEMA = "long-haul-benchmark-fact/v1"
EXPORT_SCHEMA = "long-haul-benchmark-export/v1"
COMPARISON_SCHEMA = "long-haul-benchmark-comparison/v1"


def _measurement(value: Any, unit: str) -> dict[str, Any]:
    return {"value": value, "unit": unit}


def _named_measurements(values: dict[str, Any], *, unit: str) -> dict[str, dict[str, Any]]:
    return {name: _measurement(value, unit) for name, value in values.items()}


def benchmark_fact(observation: BenchmarkObservation) -> dict[str, Any]:
    """Project one observation without reducing any scheduler identity fields."""
    measured_at = (
        observation.import_provenance.measured_at
        if observation.import_provenance is not None
        else observation.timestamp
    )
    utilization = {
        name: _measurement(
            value,
            "token/s" if name == "llama_bench_stddev_tps" else "unknown",
        )
        for name, value in observation.utilization.items()
    }
    network = {
        name: _measurement(
            value, "unknown" if isinstance(value, (int, float)) else "not_applicable"
        )
        for name, value in observation.network.items()
    }
    correlation = observation.correlation
    return {
        "schema": FACT_SCHEMA,
        "observation_id": observation.id,
        "recorded_at": observation.timestamp.isoformat(),
        "measured_at": measured_at.isoformat() if measured_at is not None else None,
        "profile": observation.profile.model_dump(mode="json"),
        "profile_identity": observation.profile.identity,
        "artifact": observation.profile.artifact.model_dump(mode="json"),
        "artifact_key": observation.profile.artifact.key,
        "runtime": (
            observation.runtime.model_dump(mode="json")
            if observation.runtime is not None
            else None
        ),
        "plan_mode": observation.plan_mode.value,
        "resources": list(observation.resources),
        "workload": {
            "name": observation.workload.name,
            "prompt_tokens": _measurement(observation.workload.prompt_tokens, "token"),
            "output_tokens": _measurement(observation.workload.output_tokens, "token"),
            "cold": observation.workload.cold,
        },
        "provenance": observation.provenance,
        "error": observation.error,
        "measurements": {
            "load_time": _measurement(observation.load_seconds, "s"),
            "time_to_first_token": _measurement(observation.ttft_seconds, "s"),
            "prefill_throughput": _measurement(observation.prefill_tps, "token/s"),
            "decode_throughput": _measurement(observation.decode_tps, "token/s"),
            "resident_memory": _named_measurements(
                observation.residency_mib, unit="MiB"
            ),
            "utilization": utilization,
            "network": network,
            "power": _measurement(observation.power_watts, "W"),
        },
        "correlation": (
            correlation.model_dump(mode="json")
            if correlation is not None
            else {
                "work_contract_id": None,
                "request_id": None,
                "compiler_attempt_id": None,
                "traceparent": None,
            }
        ),
        "source_provenance": (
            observation.import_provenance.model_dump(mode="json")
            if observation.import_provenance is not None
            else None
        ),
        "source_schema_version": observation.schema_version,
    }


def observation_from_fact(fact: dict[str, Any]) -> BenchmarkObservation:
    """Validate and rehydrate a v1 fact, preserving the source observation."""
    if fact.get("schema") != FACT_SCHEMA:
        raise ValueError("unsupported benchmark fact schema")
    profile = InferenceProfile.model_validate(fact["profile"])
    if fact.get("profile_identity") != profile.identity:
        raise ValueError("exported profile identity does not match the full profile")
    if fact.get("artifact") != profile.artifact.model_dump(mode="json"):
        raise ValueError("exported artifact does not match the full profile")
    if fact.get("artifact_key") != profile.artifact.key:
        raise ValueError("exported artifact key does not match the full artifact")

    metrics = fact["measurements"]
    _require_unit(metrics["load_time"], "s")
    _require_unit(metrics["time_to_first_token"], "s")
    _require_unit(metrics["prefill_throughput"], "token/s")
    _require_unit(metrics["decode_throughput"], "token/s")
    _require_unit(metrics["power"], "W")
    for item in metrics["resident_memory"].values():
        _require_unit(item, "MiB")
    for name, item in metrics["utilization"].items():
        expected_unit = "token/s" if name == "llama_bench_stddev_tps" else "unknown"
        _require_unit(item, expected_unit)
    for item in metrics["network"].values():
        if item.get("unit") not in {"unknown", "not_applicable"}:
            raise ValueError("unsupported network measurement unit")

    workload = fact["workload"]
    _require_unit(workload["prompt_tokens"], "token")
    _require_unit(workload["output_tokens"], "token")
    correlation_value = fact.get("correlation")
    source_value = fact.get("source_provenance")
    recorded_at = datetime.fromisoformat(fact["recorded_at"])
    measured_at_value = fact.get("measured_at")
    measured_at = (
        datetime.fromisoformat(measured_at_value)
        if measured_at_value is not None
        else None
    )
    source_provenance = (
        None
        if source_value is None
        else BenchmarkImportProvenance.model_validate(source_value)
    )
    expected_measured_at = (
        source_provenance.measured_at
        if source_provenance is not None
        else recorded_at
    )
    if measured_at != expected_measured_at:
        raise ValueError("exported measured_at does not match source measurement provenance")
    if source_provenance is not None:
        expected_id = imported_observation_id(source_provenance.source_occurrence_id)
        if fact["observation_id"] != expected_id:
            raise ValueError("imported observation ID does not match source occurrence")
    return BenchmarkObservation(
        id=fact["observation_id"],
        timestamp=recorded_at,
        profile=profile,
        runtime=(
            RuntimeIdentity.model_validate(fact["runtime"])
            if fact.get("runtime") is not None
            else None
        ),
        plan_mode=ExecutionMode(fact["plan_mode"]),
        resources=fact["resources"],
        workload=Workload(
            name=workload["name"],
            prompt_tokens=workload["prompt_tokens"]["value"],
            output_tokens=workload["output_tokens"]["value"],
            cold=workload["cold"],
        ),
        provenance=fact["provenance"],
        load_seconds=metrics["load_time"]["value"],
        ttft_seconds=metrics["time_to_first_token"]["value"],
        prefill_tps=metrics["prefill_throughput"]["value"],
        decode_tps=metrics["decode_throughput"]["value"],
        residency_mib={
            name: item["value"] for name, item in metrics["resident_memory"].items()
        },
        utilization={
            name: item["value"] for name, item in metrics["utilization"].items()
        },
        network={name: item["value"] for name, item in metrics["network"].items()},
        power_watts=metrics["power"]["value"],
        error=fact.get("error"),
        schema_version=fact.get("source_schema_version", 1),
        correlation=(
            BenchmarkCorrelation.model_validate(correlation_value)
            if correlation_value is not None and any(
                value is not None for value in correlation_value.values()
            )
            else None
        ),
        import_provenance=(
            source_provenance
        ),
    )


def _require_unit(item: dict[str, Any], unit: str) -> None:
    if item.get("unit") != unit:
        raise ValueError(f"expected measurement unit {unit}")


def build_comparison_snapshot(
    store: BenchmarkStore,
    reference_observation_id: str,
    not_before: datetime,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Export all history against the store's existing exact compatibility gate."""
    if not_before.tzinfo is None:
        raise ValueError("not_before must be timezone-aware")
    comparison_time = as_of or datetime.now(UTC)
    if comparison_time.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    observations = store.query()
    reference = next(
        (item for item in observations if item.id == reference_observation_id), None
    )
    if reference is None:
        raise ValueError("reference observation was not found in benchmark history")
    if reference.runtime is None:
        raise ValueError("reference observation has no bound RuntimeIdentity")

    facts = []
    for observation in sorted(observations, key=lambda item: item.id):
        reasons = store.explain_compatible(
            observation,
            reference.profile,
            reference.runtime,
            reference.workload,
            not_before,
            reference.plan_mode,
            as_of=comparison_time,
        )
        facts.append(
            {
                "fact": benchmark_fact(observation),
                "comparison": {
                    "query_compatible": not reasons,
                    "reasons": reasons,
                },
            }
        )
    return {
        "schema": COMPARISON_SCHEMA,
        "policy": "BenchmarkStore.query_compatible/v1",
        "query": {
            "reference_observation_id": reference.id,
            "profile": reference.profile.model_dump(mode="json"),
            "profile_identity": reference.profile.identity,
            "artifact_key": reference.profile.artifact.key,
            "runtime": reference.runtime.model_dump(mode="json"),
            "workload": reference.workload.model_dump(mode="json"),
            "plan_mode": reference.plan_mode.value,
            "resources": list(reference.profile.participating_resources),
            "not_before": not_before.isoformat(),
            "as_of": comparison_time.isoformat(),
        },
        "facts": facts,
    }


def export_benchmark_facts(
    store: BenchmarkStore,
    *,
    reference_observation_id: str | None = None,
    not_before: datetime | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Build a deterministic JSON export without requiring a database connection."""
    observations = store.query()
    facts = [benchmark_fact(item) for item in sorted(observations, key=lambda item: item.id)]
    comparison = None
    if reference_observation_id is not None:
        if not_before is None:
            raise ValueError("not_before is required when a comparison reference is supplied")
        comparison = build_comparison_snapshot(
            store, reference_observation_id, not_before, as_of=as_of
        )
    elif not_before is not None or as_of is not None:
        raise ValueError("comparison times require a reference_observation_id")
    return {
        "schema": EXPORT_SCHEMA,
        "facts": facts,
        "comparison": comparison,
    }


def write_benchmark_export(
    benchmark_path: str | Path,
    output_path: str | Path,
    *,
    reference_observation_id: str | None = None,
    not_before: datetime | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    store = BenchmarkStore(benchmark_path)
    export = export_benchmark_facts(
        store,
        reference_observation_id=reference_observation_id,
        not_before=not_before,
        as_of=as_of,
    )
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(export, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return export
