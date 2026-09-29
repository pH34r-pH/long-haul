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


def _write_vessel(tmp_path, *, cpu: bool = True):
    resources = """
  - id: ANC-C0
    kind: cpu
""" if cpu else ""
    resources += """
  - id: ANC-G0
    kind: gpu
    memory_mb: 8192
"""
    path = tmp_path / "vessel.yaml"
    path.write_text(
        "id: anchorage\nname: Anchorage\nclass_name: station\nresources:\n" + resources
    )
    return path


def _write_matrix(tmp_path, profiles):
    path = tmp_path / "matrix-report.json"
    path.write_text(
        json.dumps(
            {
                "schema": "long-haul-benchmark-matrix/v1",
                "profiles": [
                    {
                        "profile": profile.model_dump(mode="json"),
                        "measured_validation": _validation(profile),
                    }
                    for profile in profiles
                ],
            }
        )
    )
    return path


def _observation(profile, rate, *, error=None):
    return BenchmarkObservation(
        profile=profile,
        plan_mode=ExecutionMode.LOCAL,
        resources=profile.participating_resources,
        workload={"name": "exact-string"},
        provenance="measured",
        decode_tps=rate,
        error=error,
    )


def _write_benchmarks(tmp_path, observations):
    path = tmp_path / "benchmarks.jsonl"
    path.write_text("\n".join(item.model_dump_json() for item in observations) + "\n")
    return path


def test_scheduler_replay_ignores_failed_measurements_and_selects_fastest(tmp_path):
    vessel = _write_vessel(tmp_path)
    cpu = _profile("cpu", "ANC-C0")
    gpu = _profile("gpu", "ANC-G0")
    matrix = _write_matrix(tmp_path, [cpu, gpu])
    benchmarks = _write_benchmarks(
        tmp_path,
        [
            _observation(gpu, 999, error="FAIL_RUNTIME: fixture failure"),
            _observation(cpu, 20),
            _observation(gpu, 40),
        ],
    )

    result = run(str(vessel), str(matrix), str(benchmarks), workload="exact-string")

    assert result["selected"]["plan"]["inference_profile_id"] == "gpu"
    gpu_candidate = next(
        item for item in result["candidates"]
        if item["plan"]["inference_profile_id"] == "gpu"
    )
    assert gpu_candidate["decode_tps"] == 40
    assert result["observation_count"] == 3


def test_scheduler_replay_keeps_failed_only_profile_without_evidence(tmp_path):
    vessel = _write_vessel(tmp_path, cpu=False)
    gpu = _profile("gpu", "ANC-G0")
    matrix = _write_matrix(tmp_path, [gpu])
    benchmarks = _write_benchmarks(
        tmp_path,
        [_observation(gpu, 999, error="FAIL_RUNTIME: fixture failure")],
    )

    result = run(str(vessel), str(matrix), str(benchmarks))

    candidate = result["candidates"][0]
    assert candidate["evidence_id"] is None
    assert "no comparable benchmark evidence; uncertainty retained" in candidate["reasons"]
