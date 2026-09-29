"""Replay a completed benchmark matrix through the topology-aware scheduler."""
from __future__ import annotations

import json
from pathlib import Path

from .benchmarks import BenchmarkObservation
from .models import ExecutionMode, InferenceProfile
from .registry import load_vessel
from .runtime import ProfileValidation
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
) -> dict[str, object]:
    vessel = load_vessel(vessel_path)
    matrix = json.loads(Path(matrix_report_path).read_text())
    if matrix.get("schema") != "long-haul-benchmark-matrix/v1":
        raise ValueError("unsupported benchmark matrix report")
    profiles, validations = _profiles_and_validations(matrix)
    observations = _load_observations(benchmark_path, workload)
    scheduler = Scheduler([vessel], benchmarks=observations, validations=validations)
    mission = MissionRequirements(
        id=f"benchmark-replay:{workload or 'all'}",
        allow_modes={ExecutionMode.LOCAL},
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
