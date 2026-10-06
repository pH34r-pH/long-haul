from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, Field, field_validator, model_validator

from ..models import ExecutionMode, InferenceProfile
from ..runtime import RuntimeIdentity

Provenance = Literal["measured", "imported", "simulated", "estimated"]
_TRACEPARENT_V00 = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class Workload(BaseModel):
    name: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    cold: bool = False


class BenchmarkCorrelation(BaseModel):
    """Optional operational and source joins; absent IDs stay explicitly unknown."""

    work_contract_id: str | None = None
    request_id: str | None = None
    compiler_attempt_id: str | None = None
    traceparent: str | None = None

    @field_validator("traceparent")
    @classmethod
    def validate_traceparent(cls, value: str | None) -> str | None:
        if value is None:
            return value
        match = _TRACEPARENT_V00.fullmatch(value)
        if match is None:
            raise ValueError("traceparent must use the W3C Trace Context version 00 format")
        trace_id, parent_id, _flags = match.groups()
        if not int(trace_id, 16) or not int(parent_id, 16):
            raise ValueError("traceparent trace-id and parent-id must be non-zero")
        return value


class BenchmarkImportProvenance(BaseModel):
    """Replay identity for one imported source occurrence, not a run identity."""

    source_id: str = Field(min_length=1, max_length=256)
    source_sha256: str
    occurrence_index: int = Field(ge=0)
    source_occurrence_id: str
    source_repository: str | None = None
    source_commit: str | None = None
    importer_repository: str = "pH34r-pH/long-haul"
    importer_commit: str | None = None
    imported_at: datetime
    measured_at: datetime | None = None

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if _SOURCE_ID.fullmatch(value) is None:
            raise ValueError("source_id must be an opaque path-free identifier")
        return value

    @field_validator("source_sha256", "source_occurrence_id")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("source hashes must be lowercase SHA-256 hex")
        return value

    @field_validator("source_commit", "importer_commit")
    @classmethod
    def validate_commit_id(cls, value: str | None) -> str | None:
        if value is not None and _COMMIT_ID.fullmatch(value) is None:
            raise ValueError("source and importer commits must be full lowercase Git object IDs")
        return value

    @field_validator("imported_at", "measured_at")
    @classmethod
    def validate_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("import and measurement timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_occurrence_identity(self) -> BenchmarkImportProvenance:
        expected = source_occurrence_id(
            self.source_id, self.source_sha256, self.occurrence_index
        )
        if self.source_occurrence_id != expected:
            raise ValueError("source_occurrence_id does not match source, digest, and row index")
        return self


def source_occurrence_id(source_id: str, digest: str, occurrence_index: int) -> str:
    """Return a stable content-qualified identity for one source row."""
    value = json.dumps(
        ["long-haul-benchmark-source-occurrence/v1", source_id, digest, occurrence_index],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def imported_observation_id(source_occurrence: str) -> str:
    """Return the stable Long Haul record ID for an imported source occurrence."""
    return str(
        uuid5(NAMESPACE_URL, f"long-haul-benchmark-import/v1/{source_occurrence}")
    )


class BenchmarkObservation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    profile: InferenceProfile
    plan_mode: ExecutionMode
    resources: list[str]
    workload: Workload
    provenance: Provenance
    load_seconds: float | None = None
    ttft_seconds: float | None = None
    prefill_tps: float | None = None
    decode_tps: float | None = None
    residency_mib: dict[str, float] = Field(default_factory=dict)
    utilization: dict[str, float] = Field(default_factory=dict)
    network: dict[str, float | str] = Field(default_factory=dict)
    power_watts: float | None = None
    error: str | None = None
    schema_version: int = 1
    runtime: RuntimeIdentity | None = None  # unbound historical records remain readable
    correlation: BenchmarkCorrelation | None = None
    import_provenance: BenchmarkImportProvenance | None = None


def evidence_is_fresh(
    timestamp: datetime,
    not_before: datetime,
    *,
    as_of: datetime | None = None,
) -> bool:
    now = as_of or datetime.now(UTC)
    return (
        timestamp.tzinfo is not None
        and not_before.tzinfo is not None
        and now.tzinfo is not None
        and not_before <= timestamp <= now
    )


def compatibility_reasons(
    observation: BenchmarkObservation,
    profile: InferenceProfile,
    runtime: RuntimeIdentity,
    workload: Workload,
    not_before: datetime,
    mode: ExecutionMode | None = None,
    *,
    as_of: datetime | None = None,
) -> list[str]:
    """Explain the exact predicates used by ``BenchmarkStore.query_compatible``."""
    reasons = _identity_reasons(observation, profile, runtime, workload, mode)
    reasons.extend(_evidence_reasons(observation))
    reasons.extend(_freshness_reasons(observation, not_before, as_of))
    return reasons


def _identity_reasons(
    observation: BenchmarkObservation,
    profile: InferenceProfile,
    runtime: RuntimeIdentity,
    workload: Workload,
    mode: ExecutionMode | None,
) -> list[str]:
    reasons: list[str] = []
    if mode is not None and observation.plan_mode != mode:
        reasons.append("execution_mode_mismatch")
    if observation.profile.identity != profile.identity:
        reasons.append("profile_identity_mismatch")
    if observation.runtime is None:
        reasons.append("runtime_unbound")
    elif observation.runtime != runtime:
        reasons.append("runtime_identity_mismatch")
    if observation.workload != workload:
        reasons.append("workload_mismatch")
    if observation.resources != profile.participating_resources:
        reasons.append("resource_placement_mismatch")
    return reasons


def _evidence_reasons(observation: BenchmarkObservation) -> list[str]:
    reasons: list[str] = []
    if observation.provenance != "measured":
        reasons.append("provenance_not_measured")
    if observation.error is not None:
        reasons.append("run_failed")
    return reasons


def _freshness_reasons(
    observation: BenchmarkObservation,
    not_before: datetime,
    as_of: datetime | None,
) -> list[str]:
    if evidence_is_fresh(observation.timestamp, not_before, as_of=as_of):
        return []
    if observation.timestamp.tzinfo is None:
        return ["measurement_time_unknown"]
    if not_before.tzinfo is None or (as_of is not None and as_of.tzinfo is None):
        return ["freshness_boundary_unknown"]
    if observation.timestamp < not_before:
        return ["stale"]
    if as_of is not None and observation.timestamp > as_of:
        return ["future_dated"]
    return ["outside_freshness_window"]


class BenchmarkStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, observation: BenchmarkObservation) -> BenchmarkObservation:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(observation.model_dump_json() + "\n")
        return observation

    def append_imported(self, observation: BenchmarkObservation) -> BenchmarkObservation:
        """Append one imported occurrence, treating exact source replay as idempotent."""
        source = observation.import_provenance
        if source is None:
            raise ValueError("append_imported requires import_provenance")

        existing = self.query()
        for prior in existing:
            prior_source = prior.import_provenance
            if prior_source is None or prior_source.source_id != source.source_id:
                continue
            if prior_source.source_sha256 != source.source_sha256:
                raise ValueError(
                    "conflicting bytes for source_id; provide a new source_id for a new source file"
                )
            if prior_source.occurrence_index != source.occurrence_index:
                continue
            if prior_source.source_occurrence_id != source.source_occurrence_id:
                raise ValueError("conflicting source occurrence identity")
            if not _same_imported_fact(prior, observation):
                raise ValueError(
                    "conflicting benchmark fact for source occurrence; check profile and correlation"
                )
            return prior
        return self.append(observation)

    def query(
        self,
        profile_id: str | None = None,
        mode: ExecutionMode | None = None,
        resources: set[str] | None = None,
    ) -> list[BenchmarkObservation]:
        """Retrieve history; these legacy filters do not certify comparability."""
        values = (
            []
            if not self.path.exists()
            else [
                BenchmarkObservation.model_validate_json(line)
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line
            ]
        )
        return [
            observation
            for observation in values
            if (profile_id is None or observation.profile.id == profile_id)
            and (mode is None or observation.plan_mode == mode)
            and (resources is None or resources <= set(observation.resources))
        ]

    def query_compatible(
        self,
        profile: InferenceProfile,
        runtime: RuntimeIdentity,
        workload: Workload,
        not_before: datetime,
        mode: ExecutionMode | None = None,
        *,
        as_of: datetime | None = None,
    ) -> list[BenchmarkObservation]:
        """Retrieve bound successful measurements; repetition policy is a consumer gate."""
        return [
            observation
            for observation in self.query()
            if not compatibility_reasons(
                observation,
                profile,
                runtime,
                workload,
                not_before,
                mode,
                as_of=as_of,
            )
        ]


def _same_imported_fact(
    prior: BenchmarkObservation, candidate: BenchmarkObservation
) -> bool:
    left = prior.model_dump(mode="json")
    right = candidate.model_dump(mode="json")
    left.pop("id")
    right.pop("id")
    left_source = left.pop("import_provenance")
    right_source = right.pop("import_provenance")
    left_source.pop("imported_at")
    right_source.pop("imported_at")
    left_source.pop("importer_commit")
    right_source.pop("importer_commit")
    if left_source != right_source:
        return False
    if left_source.get("measured_at") is None:
        left.pop("timestamp")
        right.pop("timestamp")
    return left == right
