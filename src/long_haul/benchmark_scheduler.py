"""Replay a completed benchmark matrix through the topology-aware scheduler."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .benchmarks import BenchmarkObservation
from .benchmarks.store import Workload
from .models import ExecutionMode, InferenceProfile, ModelArtifact
from .registry import load_vessel
from .runtime import (\n    ProfileValidation,\n    RuntimeIdentity,\n    ValidationDepth,\n    ValidationState,\n)
from .scheduler import CandidateResult, MissionRequirements, Scheduler


def _load_observations(path: str | Path, workload: str | None) -> list[BenchmarkObservation]:
    values = [
        BenchmarkObservation.model_validate_json(line)
        for line in Path(path).read_text().splitlines()
        if line.strip()
    ]
    if workload is not None:
        values = [value for value in values if value.workload.name == workload]
    return values


def _profiles_and_validations(
    matrix: dict[str, object],
) -> tuple[list[InferenceProfile], list[ProfileValidation]]:
    profiles: list[InferenceProfile] = []
    validations: list[ProfileValidation] = []
    for record in matrix.get("profiles", []):
        if not isinstance(record, dict):
            continue
        profiles.append(InferenceProfile.model_validate(record["profile"]))
        measured = record.get("measured_validation")
        if measured is not None:
            validations.append(ProfileValidation.model_validate(measured))
    return profiles, validations


def _contract_runtime(record: dict[str, Any]) -> RuntimeIdentity:
    return RuntimeIdentity(
        runtime_id=record["runtime_id"],
        version=record.get("version"),
        build_id=record.get("build_id"),
        backends=record.get("backends", []),
        capabilities={
            "source_commit": record.get("source_commit", ""),
            "binary_sha256": record.get("binary_sha256", ""),
            "cuda_architectures": record.get("cuda_architectures", ""),
        },
    )


def _contract_profile(record: dict[str, Any], artifact: ModelArtifact) -> InferenceProfile:
    return InferenceProfile(
        id=record["id"],
        runtime_id=record["runtime_id"],
        strategy=record["strategy"],
        artifact=artifact,
        participating_resources=record["resources"],
        options=record.get("options", {}),
        schema_version=record.get("schema_version", 1),
    )


def _contract_validation(
    record: dict[str, Any],
    profile: InferenceProfile,
    runtime: RuntimeIdentity,
) -> ProfileValidation:
    details = record.get("validation", {})
    return ProfileValidation(
        runtime=runtime,
        profile_id=profile.id,
        artifact_key=profile.artifact.key,
        resources=profile.participating_resources,
        strategy=profile.strategy,
        options=profile.options,
        state=ValidationState(details["state"]),
        depth=ValidationDepth(details.get("depth", "BENCHMARK")),
        rationale=f"measured runtime-profile contract; {details.get('measured_runs', 0)} runs",
        provenance=details.get("provenance", "measured"),
    )


def load_profile_contract(
    path: str | Path,
) -> tuple[list[InferenceProfile], list[ProfileValidation]]:
    """Load a measured runtime-profile handoff into scheduler-native models."""
    contract: dict[str, Any] = json.loads(Path(path).read_text())
    if contract.get("schema") != "long-haul-runtime-profile/v1":
        raise ValueError("unsupported runtime profile contract")
    qualification = contract.get("qualification", {})
    if qualification.get("hardware_qualified") is not False:
        raise ValueError("runtime profile contract must preserve hardware_qualified=false")
    if qualification.get("authorization") != "operator-risk-accepted-unqualified":
        raise ValueError("runtime profile contract authorization is not accepted")
    runtime = _contract_runtime(contract["runtime"])
    model = contract["model"]
    artifact = ModelArtifact(
        foundation=model["foundation"],
        revision=model.get("revision"),
        quantization=model.get("quantization"),
    )
    profiles: list[InferenceProfile] = []
    validations: list[ProfileValidation] = []
    for record in contract.get("profiles", []):
        profile = _contract_profile(record, artifact)
        profiles.append(profile)
        validations.append(_contract_validation(record, profile, runtime))
    return profiles, validations


def _local_candidates(
    scheduler: Scheduler,
    mission: MissionRequirements,
    profiles: list[InferenceProfile],
) -> list[CandidateResult]:
    by_id = {profile.id: profile for profile in profiles}
    return [
        scheduler.evaluate(mission, plan, by_id[plan.inference_profile_id])
        for plan in scheduler.generate(profiles)
        if plan.mode is ExecutionMode.LOCAL
    ]


def _candidate_record(candidate: CandidateResult) -> dict[str, object]:
    return {
        "plan": candidate.plan.model_dump(mode="json"),
        "eligible": candidate.eligible,
        "reasons": candidate.reasons,
        "rank": candidate.rank,
        "evidence_id": candidate.evidence.id if candidate.evidence else None,
        "decode_tps": candidate.evidence.decode_tps if candidate.evidence else None,
        "prefill_tps": candidate.evidence.prefill_tps if candidate.evidence else None,
        "explanation": (
            f"comparable measured evidence {candidate.evidence.id}; ranked by "
            + ("prefill" if candidate.evidence.workload.prompt_tokens and not candidate.evidence.workload.output_tokens else "decode")
            + " throughput after eligibility constraints"
        ) if candidate.evidence else "no comparable measurement",
    }


def _selection(candidate: CandidateResult | None) -> dict[str, object] | None:
    if candidate is None:
        return None
    return {
        "plan": candidate.plan.model_dump(mode="json"),
        "reasons": candidate.reasons,
        "rank": candidate.rank,
        "evidence_id": candidate.evidence.id if candidate.evidence else None,
    }


def run(
    vessel_path: str,
    matrix_report_path: str,
    benchmark_path: str,
    *,
    workload: str | None = None,
    current_runtimes: tuple[RuntimeIdentity, ...] = (),
    workload_spec: Workload | None = None,
    evidence_not_before: datetime | None = None,
    profile_contract_path: str | Path | None = None,
) -> dict[str, object]:
    vessel = load_vessel(vessel_path)
    matrix = json.loads(Path(matrix_report_path).read_text())
    if matrix.get("schema") != "long-haul-benchmark-matrix/v1":
        raise ValueError("unsupported benchmark matrix report")
    profiles, validations = (
        load_profile_contract(profile_contract_path)
        if profile_contract_path is not None
        else _profiles_and_validations(matrix)
    )
    observations = _load_observations(benchmark_path, workload)
    scheduler = Scheduler([vessel], benchmarks=observations, validations=validations, current_runtimes=current_runtimes)
    mission = MissionRequirements(
        id=f"benchmark-replay:{workload or 'all'}",
        allow_modes={ExecutionMode.LOCAL},
        workload=workload_spec,
        evidence_not_before=evidence_not_before,
    )
    ranked = scheduler.rank(_local_candidates(scheduler, mission, profiles))
    selected = next((candidate for candidate in ranked if candidate.eligible), None)
    return {
        "schema": "long-haul-benchmark-scheduler-replay/v1",
        "vessel": vessel.id,
        "workload": workload,
        "observation_count": len(observations),
        "validation_count": len(validations),
        "candidates": [_candidate_record(candidate) for candidate in ranked],
        "selected": _selection(selected),
    }
