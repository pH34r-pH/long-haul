"""Replay a completed benchmark matrix through the topology-aware scheduler."""
from __future__ import annotations

import json
from pathlib import Path

from .benchmarks import BenchmarkObservation
from .models import ExecutionMode, InferenceProfile
from .registry import load_vessel
from .runtime import ProfileValidation
from .scheduler import MissionRequirements, Scheduler


def _load_observations(path: str | Path, workload: str | None) -> list[BenchmarkObservation]:
    values = [
        BenchmarkObservation.model_validate_json(line)
        for line in Path(path).read_text().splitlines()
        if line.strip()
    ]
    if workload is not None:
        values = [value for value in values if value.workload.name == workload]
    return values


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

    profiles: list[InferenceProfile] = []
    validations: list[ProfileValidation] = []
    for record in matrix.get("profiles", []):
        if not isinstance(record, dict):
            continue
        profile = InferenceProfile.model_validate(record["profile"])
        profiles.append(profile)
        measured = record.get("measured_validation")
        if measured is not None:
            validations.append(ProfileValidation.model_validate(measured))

    observations = _load_observations(benchmark_path, workload)
    scheduler = Scheduler(
        [vessel],
        benchmarks=observations,
        validations=validations,
    )
    mission = MissionRequirements(
        id=f"benchmark-replay:{workload or 'all'}",
        allow_modes={ExecutionMode.LOCAL},
    )
    profiles_by_id = {profile.id: profile for profile in profiles}
    candidates = []
    for plan in scheduler.generate(profiles):
        if plan.mode is not ExecutionMode.LOCAL:
            continue
        result = scheduler.evaluate(
            mission,
            plan,
            profiles_by_id[plan.inference_profile_id],
        )
        candidates.append(result)

    ranked = scheduler.rank(candidates)
    eligible = [candidate for candidate in ranked if candidate.eligible]
    selected = eligible[0] if eligible else None
    output: dict[str, object] = {
        "schema": "long-haul-benchmark-scheduler-replay/v1",
        "vessel": vessel.id,
        "workload": workload,
        "observation_count": len(observations),
        "validation_count": len(validations),
        "candidates": [
            {
                "plan": candidate.plan.model_dump(mode="json"),
                "eligible": candidate.eligible,
                "reasons": candidate.reasons,
                "rank": candidate.rank,
                "evidence_id": candidate.evidence.id if candidate.evidence else None,
                "decode_tps": candidate.evidence.decode_tps if candidate.evidence else None,
            }
            for candidate in ranked
        ],
        "selected": (
            {
                "plan": selected.plan.model_dump(mode="json"),
                "reasons": selected.reasons,
                "rank": selected.rank,
                "evidence_id": selected.evidence.id if selected.evidence else None,
            }
            if selected
            else None
        ),
    }
    return output
