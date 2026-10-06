from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from long_haul.benchmarks import (
    BenchmarkCorrelation,
    BenchmarkImportProvenance,
    BenchmarkObservation,
    BenchmarkStore,
    Workload,
    imported_observation_id,
    source_occurrence_id,
)
from long_haul.benchmarks.export import (
    benchmark_fact,
    build_comparison_snapshot,
    export_benchmark_facts,
    observation_from_fact,
)
from long_haul.models import ExecutionMode, InferenceProfile, ModelArtifact
from long_haul.runtime import RuntimeIdentity

FIXTURE_DIR = Path(__file__).parent / "fixtures"
AS_OF = datetime(2026, 3, 1, tzinfo=UTC)
NOT_BEFORE = datetime(2026, 1, 1, tzinfo=UTC)


def _profile(*, artifact: str = "synthetic/model", resources: list[str] | None = None):
    return InferenceProfile(
        id="synthetic-profile",
        runtime_id="llama.cpp",
        strategy="resident",
        artifact=ModelArtifact(
            foundation=artifact,
            revision="synthetic-revision",
            quantization="Q4_K_M",
            adapters=["adapter-a", "adapter-b"],
            auxiliary_artifacts=["vocab-a", "vocab-b"],
        ),
        participating_resources=resources or ["gpu-0", "cpu-0"],
        options={"batch": 8, "model_path": "synthetic.gguf"},
    )


def _runtime(**capabilities):
    return RuntimeIdentity(
        runtime_id="llama.cpp",
        version="b11146",
        build_id="synthetic-commit",
        binary_path="/synthetic/llama-bench",
        backends=["CUDA", "CPU"],
        capabilities={"binary_sha256": "a" * 64, **capabilities},
    )


def _observation(
    observation_id: str,
    **overrides: Any,
) -> BenchmarkObservation:
    selected_profile = overrides.pop("profile", None) or _profile()
    selected_runtime = overrides.pop("runtime", "default")
    if selected_runtime == "default":
        selected_runtime = _runtime()
    selected_workload = overrides.pop("workload", None) or Workload(
        name="synthetic-prompt-4-output-8",
        prompt_tokens=4,
        output_tokens=8,
        cold=True,
    )
    selected_resources = overrides.pop("resources", None)
    values = {
        "id": observation_id,
        "timestamp": overrides.pop("timestamp", datetime(2026, 2, 1, tzinfo=UTC)),
        "profile": selected_profile,
        "runtime": selected_runtime,
        "plan_mode": ExecutionMode.LOCAL,
        "resources": selected_resources or selected_profile.participating_resources.copy(),
        "workload": selected_workload,
        "provenance": "measured",
        "load_seconds": 0.125,
        "ttft_seconds": 0.25,
        "prefill_tps": 128.0,
        "decode_tps": 32.0,
        "residency_mib": {"gpu-0": 512.5},
        "utilization": {"gpu_percent": 75.0, "llama_bench_stddev_tps": 1.25},
        "network": {"throughput": 4.5, "path": "synthetic-local"},
        "power_watts": 42.0,
        "error": None,
        "correlation": None,
    }
    values.update(overrides)
    return BenchmarkObservation(**values)


def _save_store(path, observations):
    store = BenchmarkStore(path)
    for observation in observations:
        store.append(observation)
    return store


def _comparison_summary(report):
    return {
        "policy": report["policy"],
        "runs": [
            {
                "observation_id": item["fact"]["observation_id"],
                **item["comparison"],
            }
            for item in report["facts"]
        ],
    }


def test_export_import_round_trip_preserves_exact_identity_and_source_facts():
    observation = _observation(
        "round-trip",
        correlation=BenchmarkCorrelation(
            work_contract_id="contract-184",
            request_id="request-unique",
            compiler_attempt_id="compiler-attempt-7",
            traceparent="00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        ),
    )

    fact = benchmark_fact(observation)
    restored = observation_from_fact(fact)

    assert restored == observation
    assert fact["profile"] == observation.profile.model_dump(mode="json")
    assert fact["profile_identity"] == observation.profile.identity
    assert fact["artifact_key"] == observation.profile.artifact.key
    assert fact["runtime"] == observation.runtime.model_dump(mode="json")
    assert fact["resources"] == ["gpu-0", "cpu-0"]
    assert fact["workload"] == {
        "name": "synthetic-prompt-4-output-8",
        "prompt_tokens": {"value": 4, "unit": "token"},
        "output_tokens": {"value": 8, "unit": "token"},
        "cold": True,
    }
    assert fact["measurements"]["load_time"] == {"value": 0.125, "unit": "s"}
    assert fact["measurements"]["decode_throughput"] == {
        "value": 32.0,
        "unit": "token/s",
    }
    assert fact["measurements"]["resident_memory"]["gpu-0"] == {
        "value": 512.5,
        "unit": "MiB",
    }
    assert fact["measurements"]["power"] == {"value": 42.0, "unit": "W"}


def test_imported_fact_round_trip_keeps_unknown_measurement_time_separate():
    imported_at = datetime(2026, 2, 1, tzinfo=UTC)
    source_id = "synthetic-native-source"
    source_digest = "b" * 64
    source = BenchmarkImportProvenance(
        source_id=source_id,
        source_sha256=source_digest,
        occurrence_index=2,
        source_occurrence_id=source_occurrence_id(source_id, source_digest, 2),
        source_repository="synthetic/producer",
        source_commit="c" * 40,
        importer_commit="d" * 40,
        imported_at=imported_at,
        measured_at=None,
    )
    observation = _observation(
        imported_observation_id(source.source_occurrence_id), timestamp=imported_at
    )
    observation.provenance = "imported"
    observation.import_provenance = source

    fact = benchmark_fact(observation)
    restored = observation_from_fact(fact)

    assert restored == observation
    assert fact["measured_at"] is None
    assert fact["recorded_at"] == imported_at.isoformat()
    assert fact["source_provenance"]["source_commit"] == "c" * 40
    assert fact["source_provenance"]["importer_commit"] == "d" * 40
    assert fact["runtime"]["build_id"] == "synthetic-commit"


def test_export_keeps_unknown_source_and_correlation_fields_explicit():
    observation = _observation("unbound-source")
    fact = benchmark_fact(observation)

    assert fact["source_provenance"] is None
    assert fact["correlation"] == {
        "work_contract_id": None,
        "request_id": None,
        "compiler_attempt_id": None,
        "traceparent": None,
    }


@pytest.mark.parametrize(
    "value",
    [
        "00-00000000000000000000000000000000-b7ad6b7169203331-01",
        "00-0af7651916cd43dd8448eb211c80319c-0000000000000000-01",
        "00-0AF7651916CD43DD8448EB211C80319C-b7ad6b7169203331-01",
        "ff-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "00-not-a-trace-not-a-span-01",
    ],
)
def test_traceparent_rejects_invalid_w3c_context(value):
    with pytest.raises(ValueError, match="traceparent"):
        BenchmarkCorrelation(traceparent=value)


def test_exported_profile_tampering_fails_import_validation():
    fact = benchmark_fact(_observation("tampered"))
    fact["profile"]["options"]["batch"] = 16
    with pytest.raises(ValueError, match="profile identity"):
        observation_from_fact(fact)


def test_saved_synthetic_comparison_fixture_uses_existing_compatibility_policy(tmp_path):
    reference = _observation("reference")
    replicate = _observation("replicate")
    unbound_legacy = _observation("legacy-unbound", runtime=None)
    store = _save_store(
        tmp_path / "benchmarks.jsonl", [reference, replicate, unbound_legacy]
    )

    report = build_comparison_snapshot(
        store, "reference", NOT_BEFORE, as_of=AS_OF
    )
    summary = _comparison_summary(report)
    expected = json.loads(
        (FIXTURE_DIR / "benchmark-comparison-summary.json").read_text()
    )

    assert expected["fixture_provenance"] == "synthetic"
    assert {"policy": summary["policy"], "runs": summary["runs"]} == {
        "policy": expected["policy"],
        "runs": expected["runs"],
    }
    assert [item["observation_id"] for item in summary["runs"]].count("replicate") == 1
    assert len([item for item in summary["runs"] if item["query_compatible"]]) == 2


def _comparison_results(tmp_path, rows):
    store = _save_store(tmp_path / "parity.jsonl", rows)
    reference = rows[0]
    assert reference.runtime is not None
    report = build_comparison_snapshot(store, reference.id, NOT_BEFORE, as_of=AS_OF)
    exported = {
        run["fact"]["observation_id"]
        for run in report["facts"]
        if run["comparison"]["query_compatible"]
    }
    selected = {
        observation.id
        for observation in store.query_compatible(
            reference.profile,
            reference.runtime,
            reference.workload,
            NOT_BEFORE,
            reference.plan_mode,
            as_of=AS_OF,
        )
    }
    reason_by_id = {
        run["fact"]["observation_id"]: run["comparison"]["reasons"]
        for run in report["facts"]
    }
    assert exported == selected
    return exported, reason_by_id


def test_comparison_labels_match_query_for_identity_and_workload_gates(tmp_path):
    profile = _profile()
    workload = Workload(
        name="synthetic-prompt-4-output-8", prompt_tokens=4, output_tokens=8, cold=True
    )
    reference = _observation("reference", profile=profile, workload=workload)
    rows = [
        reference,
        _observation("independent-repeat", profile=profile, workload=workload),
        _observation(
            "artifact-change",
            profile=_profile(artifact="synthetic/other-model"),
            workload=workload,
        ),
        _observation(
            "runtime-change",
            profile=profile,
            workload=workload,
            runtime=_runtime(extra_capability="true"),
        ),
        _observation(
            "placement-change",
            profile=profile,
            workload=workload,
            resources=["cpu-0", "gpu-0"],
        ),
        _observation(
            "prompt-token-change",
            profile=profile,
            workload=Workload(
                name=workload.name,
                prompt_tokens=5,
                output_tokens=8,
                cold=True,
            ),
        ),
        _observation(
            "cold-state-change",
            profile=profile,
            workload=Workload(
                name=workload.name,
                prompt_tokens=4,
                output_tokens=8,
                cold=False,
            ),
        ),
    ]
    exported, reason_by_id = _comparison_results(tmp_path, rows)

    assert exported == {"reference", "independent-repeat"}
    assert reason_by_id["artifact-change"] == ["profile_identity_mismatch"]
    assert reason_by_id["runtime-change"] == ["runtime_identity_mismatch"]
    assert reason_by_id["placement-change"] == ["resource_placement_mismatch"]
    assert reason_by_id["prompt-token-change"] == ["workload_mismatch"]
    assert reason_by_id["cold-state-change"] == ["workload_mismatch"]


def test_comparison_labels_match_query_for_evidence_and_time_gates(tmp_path):
    profile = _profile()
    workload = Workload(
        name="synthetic-prompt-4-output-8", prompt_tokens=4, output_tokens=8, cold=True
    )
    rows = [
        _observation("reference", profile=profile, workload=workload),
        _observation("independent-repeat", profile=profile, workload=workload),
        _observation(
            "stale",
            profile=profile,
            workload=workload,
            timestamp=datetime(2025, 12, 31, tzinfo=UTC),
        ),
        _observation(
            "future",
            profile=profile,
            workload=workload,
            timestamp=datetime(2026, 3, 2, tzinfo=UTC),
        ),
        _observation("failed", profile=profile, workload=workload, error="synthetic failure"),
        _observation("imported", profile=profile, workload=workload, provenance="imported"),
        _observation("simulated", profile=profile, workload=workload, provenance="simulated"),
        _observation("legacy-unbound", profile=profile, workload=workload, runtime=None),
        _observation(
            "mode-change",
            profile=profile,
            workload=workload,
            plan_mode=ExecutionMode.POOL,
        ),
    ]
    exported, reason_by_id = _comparison_results(tmp_path, rows)

    assert exported == {"reference", "independent-repeat"}
    assert reason_by_id["stale"] == ["stale"]
    assert reason_by_id["future"] == ["future_dated"]
    assert reason_by_id["failed"] == ["run_failed"]
    assert reason_by_id["imported"] == ["provenance_not_measured"]
    assert reason_by_id["simulated"] == ["provenance_not_measured"]
    assert reason_by_id["legacy-unbound"] == ["runtime_unbound"]
    assert reason_by_id["mode-change"] == ["execution_mode_mismatch"]


def test_reference_and_comparison_cutoffs_must_be_explicitly_bound(tmp_path):
    store = _save_store(tmp_path / "single.jsonl", [_observation("reference")])
    with pytest.raises(ValueError, match="timezone-aware"):
        build_comparison_snapshot(
            store, "reference", datetime.fromisoformat("2026-01-01"), as_of=AS_OF
        )
    with pytest.raises(ValueError, match="RuntimeIdentity"):
        unbound = _save_store(
            tmp_path / "unbound.jsonl", [_observation("legacy", runtime=None)]
        )
        build_comparison_snapshot(unbound, "legacy", NOT_BEFORE, as_of=AS_OF)


def test_export_envelope_has_all_facts_and_optional_comparison(tmp_path):
    store = _save_store(
        tmp_path / "history.jsonl",
        [_observation("reference"), _observation("replicate")],
    )

    facts_only = export_benchmark_facts(store)
    compared = export_benchmark_facts(
        store,
        reference_observation_id="reference",
        not_before=NOT_BEFORE,
        as_of=AS_OF,
    )

    assert facts_only["schema"] == "long-haul-benchmark-export/v1"
    assert [fact["observation_id"] for fact in facts_only["facts"]] == [
        "reference",
        "replicate",
    ]
    assert facts_only["comparison"] is None
    assert compared["comparison"]["schema"] == "long-haul-benchmark-comparison/v1"
    assert compared["comparison"]["policy"] == "BenchmarkStore.query_compatible/v1"
