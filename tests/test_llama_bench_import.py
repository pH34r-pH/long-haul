import json
from datetime import UTC, datetime

import pytest

from long_haul.benchmarks import BenchmarkCorrelation
from long_haul.llama_bench_import import LlamaBenchImportOptions, import_profile
from long_haul.runtime import RuntimeIdentity


def _write_and_check_fixture(tmp_path):
    manifest = tmp_path / "matrix.yaml"
    manifest.write_text(
        """
artifact:
  foundation: fixture/model
  quantization: Q4_K_M
profiles:
  - id: anc-g0
    strategy: gpu_offload
    resources: [ANC-G0]
    explore_unknown: true
    options:
      gpu_layers: 999
      cuda_visible_devices: "0"
workloads:
  - name: smoke
    prompt: hello
""".lstrip()
    )
    raw = tmp_path / "raw.json"
    raw.write_text(
        json.dumps(
            [
                {
                    "build_commit": "abcdef1234",
                    "build_number": 11146,
                    "backends": "CUDA",
                    "model_filename": "fixture.gguf",
                    "n_gpu_layers": -1,
                    "split_mode": "layer",
                    "main_gpu": 0,
                    "tensor_split": "0.00",
                    "n_prompt": 512,
                    "n_gen": 0,
                    "n_depth": 0,
                    "avg_ts": 321.5,
                    "stddev_ts": 4.2,
                },
                {
                    "build_commit": "abcdef1234",
                    "build_number": 11146,
                    "backends": "CUDA",
                    "model_filename": "fixture.gguf",
                    "n_gpu_layers": -1,
                    "split_mode": "layer",
                    "main_gpu": 0,
                    "tensor_split": "0.00",
                    "n_prompt": 0,
                    "n_gen": 128,
                    "n_depth": 0,
                    "avg_ts": 22.5,
                    "stddev_ts": 0.5,
                },
            ]
        )
    )

    record = import_profile(manifest, "anc-g0", raw, tmp_path / "out")

    assert record["measured_validation"]["state"] == "SUPPORTED"
    assert record["measured_validation"]["depth"] == "BENCHMARK"
    lines = (tmp_path / "out" / "benchmarks.jsonl").read_text().splitlines()
    assert len(lines) == 2
    observations = [json.loads(line) for line in lines]
    assert observations[0]["workload"]["name"] == "pp512"
    assert observations[0]["prefill_tps"] == 321.5
    assert observations[1]["workload"]["name"] == "tg128"
    assert observations[1]["decode_tps"] == 22.5
    report = json.loads((tmp_path / "out" / "matrix-report.json").read_text())
    assert report["profiles"][0]["profile"]["id"] == "anc-g0"
    assert report["profiles"][0]["measured_validation"]["runtime"]["build_id"] == "abcdef1234"
    assert [row["provenance"] for row in observations] == ["imported", "imported"]
    assert all(row["import_provenance"]["measured_at"] is None for row in observations)


def test_import_native_llama_bench_updates_matrix_report_and_store(tmp_path):
    _write_and_check_fixture(tmp_path)



def test_import_retains_exact_runtime_identity_and_measurement_time(tmp_path):
    _write_and_check_fixture(tmp_path)
    manifest = tmp_path / "matrix.yaml"
    raw = tmp_path / "raw.json"
    identity = RuntimeIdentity(runtime_id="llama.cpp", version="b11146", build_id="source:binary:sm61", capabilities={"binary_sha256": "a" * 64})
    measured_at = datetime(2026, 9, 30, tzinfo=UTC)
    source_commit = "c" * 40
    importer_commit = "d" * 40
    bound = import_profile(
        manifest,
        "anc-g0",
        raw,
        tmp_path / "bound",
        options=LlamaBenchImportOptions(
            runtime_identity=identity,
            measured_at=measured_at,
            source_id="native-run-01",
            source_repository="synthetic/producer",
            source_commit=source_commit,
            importer_commit=importer_commit,
            correlation=BenchmarkCorrelation(request_id="native-request-01"),
        ),
    )
    rows = [json.loads(line) for line in (tmp_path / "bound" / "benchmarks.jsonl").read_text().splitlines()]
    assert rows[0]["runtime"] == identity.model_dump(mode="json")
    assert datetime.fromisoformat(rows[0]["timestamp"]) == measured_at
    assert bound["measured_validation"]["runtime"] == identity.model_dump(mode="json")
    assert rows[0]["provenance"] == "measured"
    assert rows[0]["import_provenance"]["measured_at"] == measured_at.isoformat().replace(
        "+00:00", "Z"
    )
    assert rows[0]["import_provenance"]["source_commit"] == source_commit
    assert rows[0]["import_provenance"]["importer_commit"] == importer_commit
    assert rows[0]["runtime"]["build_id"] == "source:binary:sm61"
    assert rows[0]["correlation"]["request_id"] == "native-request-01"


def test_reimport_is_idempotent_and_reports_stable_source_occurrences(tmp_path):
    _write_and_check_fixture(tmp_path)
    manifest = tmp_path / "matrix.yaml"
    raw = tmp_path / "raw.json"
    output = tmp_path / "idempotent"

    options = LlamaBenchImportOptions(source_id="bench-run-a")
    first = import_profile(manifest, "anc-g0", raw, output, options=options)
    first_bytes = (output / "benchmarks.jsonl").read_bytes()
    first_report = (output / "matrix-report.json").read_bytes()
    second = import_profile(manifest, "anc-g0", raw, output, options=options)

    assert first == second
    assert (output / "benchmarks.jsonl").read_bytes() == first_bytes
    assert (output / "matrix-report.json").read_bytes() == first_report
    assert [run["source_occurrence_id"] for run in first["runs"]] == [
        run["source_occurrence_id"] for run in second["runs"]
    ]


def test_distinct_source_ids_preserve_identical_independent_measurements(tmp_path):
    _write_and_check_fixture(tmp_path)
    manifest = tmp_path / "matrix.yaml"
    raw = tmp_path / "raw.json"
    output = tmp_path / "replicates"
    measured_at = datetime(2026, 9, 30, tzinfo=UTC)

    first = import_profile(
        manifest,
        "anc-g0",
        raw,
        output,
        options=LlamaBenchImportOptions(
            source_id="acquisition-a", measured_at=measured_at
        ),
    )
    second = import_profile(
        manifest,
        "anc-g0",
        raw,
        output,
        options=LlamaBenchImportOptions(
            source_id="acquisition-b", measured_at=measured_at
        ),
    )
    rows = [
        json.loads(line)
        for line in (output / "benchmarks.jsonl").read_text().splitlines()
    ]

    assert len(rows) == 4
    assert {run["observation_id"] for run in first["runs"]}.isdisjoint(
        {run["observation_id"] for run in second["runs"]}
    )
    assert [row["decode_tps"] for row in rows].count(22.5) == 2


def test_same_source_id_with_changed_bytes_fails_visibly(tmp_path):
    _write_and_check_fixture(tmp_path)
    manifest = tmp_path / "matrix.yaml"
    raw = tmp_path / "raw.json"
    output = tmp_path / "conflict"
    options = LlamaBenchImportOptions(source_id="immutable-run")
    import_profile(manifest, "anc-g0", raw, output, options=options)

    value = json.loads(raw.read_text())
    value[0]["avg_ts"] += 1
    raw.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="conflicting bytes for source_id"):
        import_profile(manifest, "anc-g0", raw, output, options=options)
