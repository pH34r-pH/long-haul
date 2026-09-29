import json

from long_haul.benchmark_scheduler import run
from long_haul.benchmarks import BenchmarkObservation
from long_haul.models import ExecutionMode, InferenceProfile, ModelArtifact
from long_haul.runtime import ProfileValidation, RuntimeIdentity, ValidationState


def _profile(profile_id: str, resource: str) -> InferenceProfile:
    return InferenceProfile(
        id=profile_id,
        runtime_id="llama.cpp",
        strategy="resident",
        artifact=ModelArtifact(foundation="fixture", quantization="Q4"),
        participating_resources=[resource],
        options={"model_path": "fixture.gguf"},
    )


def _validation(profile: InferenceProfile) -> dict:
    return ProfileValidation(
        runtime=RuntimeIdentity(runtime_id="llama.cpp", build_id="fixture"),
        profile_id=profile.id,
        artifact_key=profile.artifact.key,
        resources=profile.participating_resources,
        strategy=profile.strategy,
        options=profile.options,
        state=ValidationState.SUPPORTED,
        rationale="measured fixture",
        provenance="measured",
    ).model_dump(mode="json")


def test_scheduler_replay_ignores_failed_measurements_and_selects_fastest(tmp_path):
    vessel = tmp_path / "vessel.yaml"
    vessel.write_text(
        """
id: anchorage
name: Anchorage
class_name: station
resources:
  - id: ANC-C0
    kind: cpu
  - id: ANC-G0
    kind: gpu
    memory_mb: 8192
""".lstrip()
    )

    cpu = _profile("cpu", "ANC-C0")
    gpu = _profile("gpu", "ANC-G0")
    matrix = tmp_path / "matrix-report.json"
    matrix.write_text(
        json.dumps(
            {
                "schema": "long-haul-benchmark-matrix/v1",
                "profiles": [
                    {
                        "profile": cpu.model_dump(mode="json"),
                        "measured_validation": _validation(cpu),
                    },
                    {
                        "profile": gpu.model_dump(mode="json"),
                        "measured_validation": _validation(gpu),
                    },
                ],
            }
        )
    )

    benchmarks = tmp_path / "benchmarks.jsonl"
    observations = [
        BenchmarkObservation(
            profile=gpu,
            plan_mode=ExecutionMode.LOCAL,
            resources=["ANC-G0"],
            workload={"name": "exact-string"},
            provenance="measured",
            decode_tps=999,
            error="FAIL_RUNTIME: fixture failure",
        ),
        BenchmarkObservation(
            profile=cpu,
            plan_mode=ExecutionMode.LOCAL,
            resources=["ANC-C0"],
            workload={"name": "exact-string"},
            provenance="measured",
            decode_tps=20,
        ),
        BenchmarkObservation(
            profile=gpu,
            plan_mode=ExecutionMode.LOCAL,
            resources=["ANC-G0"],
            workload={"name": "exact-string"},
            provenance="measured",
            decode_tps=40,
        ),
    ]
    benchmarks.write_text("\n".join(item.model_dump_json() for item in observations) + "\n")

    result = run(
        str(vessel),
        str(matrix),
        str(benchmarks),
        workload="exact-string",
    )

    assert result["selected"]["plan"]["inference_profile_id"] == "gpu"
    gpu_candidate = next(
        item for item in result["candidates"]
        if item["plan"]["inference_profile_id"] == "gpu"
    )
    assert gpu_candidate["decode_tps"] == 40
    assert result["observation_count"] == 3


def test_scheduler_replay_keeps_failed_only_profile_without_evidence(tmp_path):
    vessel = tmp_path / "vessel.yaml"
    vessel.write_text(
        """
id: anchorage
name: Anchorage
class_name: station
resources:
  - id: ANC-G0
    kind: gpu
    memory_mb: 8192
""".lstrip()
    )
    gpu = _profile("gpu", "ANC-G0")
    matrix = tmp_path / "matrix-report.json"
    matrix.write_text(
        json.dumps(
            {
                "schema": "long-haul-benchmark-matrix/v1",
                "profiles": [
                    {
                        "profile": gpu.model_dump(mode="json"),
                        "measured_validation": _validation(gpu),
                    }
                ],
            }
        )
    )
    failed = BenchmarkObservation(
        profile=gpu,
        plan_mode=ExecutionMode.LOCAL,
        resources=["ANC-G0"],
        workload={"name": "exact-string"},
        provenance="measured",
        decode_tps=999,
        error="FAIL_RUNTIME: fixture failure",
    )
    benchmarks = tmp_path / "benchmarks.jsonl"
    benchmarks.write_text(failed.model_dump_json() + "\n")

    result = run(str(vessel), str(matrix), str(benchmarks))
    candidate = result["candidates"][0]
    assert candidate["evidence_id"] is None
    assert "no comparable benchmark evidence; uncertainty retained" in candidate["reasons"]
