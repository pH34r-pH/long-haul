import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from long_haul.adapters.llama_cpp import LlamaCppAdapter
from long_haul.benchmark_matrix import run
from long_haul.models import InferenceProfile, ModelArtifact
from long_haul.runtime import ExecutionRequest, FailureClass, ValidationState

pytestmark = pytest.mark.skipif(os.name != "posix", reason="fixture executable uses a POSIX shebang")


@pytest.fixture
def fixture_runtime(tmp_path: Path) -> Path:
    path = tmp_path / "llama-cli"
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if '--version' in sys.argv:\n"
        "    print('fixture llama.cpp 1.0')\n"
        "else:\n"
        "    print('fixture output')\n"
        "    print('llama_perf_context_print: load time = 125.00 ms', file=sys.stderr)\n"
        "    print('llama_perf_context_print: prompt eval time = 20.00 ms / 4 tokens (200.00 tokens per second)', file=sys.stderr)\n"
        "    print('llama_perf_context_print: eval time = 80.00 ms / 8 runs (100.00 tokens per second)', file=sys.stderr)\n"
    )
    path.chmod(0o700)
    return path


def accelerated_profile(tmp_path: Path) -> InferenceProfile:
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fixture")
    return InferenceProfile(
        id="anc-g1",
        runtime_id="llama.cpp",
        strategy="gpu_offload",
        artifact=ModelArtifact(foundation="fixture", quantization="Q4"),
        participating_resources=["ANC-G1"],
        options={
            "model_path": str(model),
            "gpu_layers": 20,
            "cuda_visible_devices": "1",
        },
    )


def test_accelerated_profile_requires_explicit_exploration(fixture_runtime, tmp_path):
    profile = accelerated_profile(tmp_path)
    adapter = LlamaCppAdapter(fixture_runtime)

    validation = adapter.validate(profile)
    assert validation.state is ValidationState.UNKNOWN

    denied = adapter.execute(
        ExecutionRequest(request_id="denied", profile=profile, prompt="test")
    )
    assert denied.success is False
    assert denied.error_class is FailureClass.RUNTIME

    measured = adapter.execute(
        ExecutionRequest(
            request_id="measured",
            profile=profile,
            prompt="test",
            allow_unknown_runtime=True,
        )
    )
    assert measured.success is True
    assert "cuda" in measured.runtime.backends
    assert measured.runtime.capabilities["accelerated_execution"] == "measured"
    assert measured.timings.load_seconds == pytest.approx(0.125)
    assert measured.timings.prompt_tokens == 4
    assert measured.timings.generated_tokens == 8
    assert measured.timings.prefill_tps == pytest.approx(200)
    assert measured.timings.decode_tps == pytest.approx(100)


def test_manifest_matrix_records_cpu_and_exploratory_gpu(fixture_runtime, tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fixture")
    manifest = tmp_path / "matrix.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "artifact": {"foundation": "fixture", "quantization": "Q4"},
                "profiles": [
                    {
                        "id": "anc-cpu",
                        "strategy": "resident",
                        "resources": ["ANC-C0"],
                    },
                    {
                        "id": "anc-g0",
                        "strategy": "gpu_offload",
                        "resources": ["ANC-G0"],
                        "explore_unknown": True,
                        "options": {
                            "gpu_layers": 20,
                            "cuda_visible_devices": "0",
                        },
                    },
                ],
                "workloads": [
                    {
                        "name": "short",
                        "prompt": "Return exactly: LONG_HAUL_OK",
                        "max_tokens": 8,
                    }
                ],
            }
        )
    )

    output = tmp_path / "result"
    report = run(
        str(fixture_runtime),
        str(model),
        str(manifest),
        str(output),
        timeout_seconds=5,
    )

    assert report["schema"] == "long-haul-benchmark-matrix/v1"
    lines = (output / "benchmarks.jsonl").read_text().splitlines()
    assert len(lines) == 2
    observations = [json.loads(line) for line in lines]
    assert {item["profile"]["id"] for item in observations} == {"anc-cpu", "anc-g0"}
    gpu = next(item for item in report["profiles"] if item["profile"]["id"] == "anc-g0")
    assert gpu["preflight"]["state"] == "UNKNOWN"
    assert gpu["measured_validation"]["state"] == "SUPPORTED"


def test_llama_command_exposes_cpu_affinity_and_moe_controls(fixture_runtime, tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fixture")
    profile = InferenceProfile(
        id="anc-hybrid-affinity",
        runtime_id="llama.cpp",
        strategy="gpu_offload",
        artifact=ModelArtifact(foundation="fixture", quantization="Q4"),
        participating_resources=["ANC-C0", "ANC-G0"],
        options={
            "model_path": str(model),
            "gpu_layers": 20,
            "threads": 6,
            "threads_batch": 12,
            "cpu_range": "0-5",
            "cpu_range_batch": "0-11",
            "cpu_strict": True,
            "cpu_strict_batch": True,
            "n_cpu_moe": 8,
        },
    )
    adapter = LlamaCppAdapter(fixture_runtime)
    command = adapter._command(
        ExecutionRequest(request_id="affinity", profile=profile, prompt="test")
    )

    expected_pairs = {
        "--threads": "6",
        "--threads-batch": "12",
        "--cpu-range": "0-5",
        "--cpu-range-batch": "0-11",
        "--cpu-strict": "1",
        "--cpu-strict-batch": "1",
        "--n-cpu-moe": "8",
    }
    for flag, value in expected_pairs.items():
        position = command.index(flag)
        assert command[position + 1] == value


def test_llama_command_exposes_all_cpu_moe_flag(fixture_runtime, tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fixture")
    profile = InferenceProfile(
        id="anc-cpu-moe",
        runtime_id="llama.cpp",
        strategy="gpu_offload",
        artifact=ModelArtifact(foundation="fixture", quantization="Q4"),
        participating_resources=["ANC-C0", "ANC-G0"],
        options={
            "model_path": str(model),
            "gpu_layers": 20,
            "cpu_moe": True,
        },
    )
    command = LlamaCppAdapter(fixture_runtime)._command(
        ExecutionRequest(request_id="cpu-moe", profile=profile, prompt="test")
    )
    assert "--cpu-moe" in command


def test_llama_command_exposes_speculative_placement_controls(fixture_runtime, tmp_path):
    model = tmp_path / "model.gguf"
    draft = tmp_path / "draft.gguf"
    model.write_bytes(b"fixture")
    draft.write_bytes(b"draft")
    profile = InferenceProfile(
        id="anc-speculative",
        runtime_id="llama.cpp",
        strategy="gpu_offload",
        artifact=ModelArtifact(foundation="fixture", quantization="Q4"),
        participating_resources=["ANC-C0", "ANC-G0", "ANC-G1"],
        options={
            "model_path": str(model),
            "gpu_layers": 20,
            "spec_type": "draft-simple",
            "draft_model_path": str(draft),
            "draft_device": "CUDA1",
            "draft_gpu_layers": "all",
            "draft_threads": 6,
            "draft_threads_batch": 12,
            "draft_cpu_range": "6-11",
            "draft_n_max": 5,
            "draft_n_min": 1,
            "draft_n_cpu_moe": 4,
        },
    )
    command = LlamaCppAdapter(fixture_runtime)._command(
        ExecutionRequest(request_id="spec", profile=profile, prompt="test")
    )
    expected_pairs = {
        "--spec-type": "draft-simple",
        "--spec-draft-model": str(draft),
        "--spec-draft-device": "CUDA1",
        "--spec-draft-ngl": "all",
        "--spec-draft-threads": "6",
        "--spec-draft-threads-batch": "12",
        "--spec-draft-cpu-range": "6-11",
        "--spec-draft-n-max": "5",
        "--spec-draft-n-min": "1",
        "--spec-draft-n-cpu-moe": "4",
    }
    for flag, value in expected_pairs.items():
        position = command.index(flag)
        assert command[position + 1] == value


def test_llama_command_exposes_ngram_speculation_without_draft_model(fixture_runtime, tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fixture")
    profile = InferenceProfile(
        id="anc-ngram-spec",
        runtime_id="llama.cpp",
        strategy="resident",
        artifact=ModelArtifact(foundation="fixture", quantization="Q4"),
        participating_resources=["ANC-C0"],
        options={
            "model_path": str(model),
            "spec_type": "ngram-simple",
        },
    )
    command = LlamaCppAdapter(fixture_runtime)._command(
        ExecutionRequest(request_id="ngram", profile=profile, prompt="test")
    )
    assert command[command.index("--spec-type") + 1] == "ngram-simple"
    assert "--spec-draft-model" not in command
