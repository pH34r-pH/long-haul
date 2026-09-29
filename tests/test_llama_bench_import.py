import json

from long_haul.llama_bench_import import import_profile


def test_import_native_llama_bench_updates_matrix_report_and_store(tmp_path):
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
