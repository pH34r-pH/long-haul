from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from ..models import ExecutionMode, InferenceProfile
from ..runtime import RuntimeIdentity

Provenance = Literal["measured", "imported", "simulated", "estimated"]
class Workload(BaseModel):
    name: str; prompt_tokens: int = 0; output_tokens: int = 0; cold: bool = False
class BenchmarkObservation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4())); timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    profile: InferenceProfile; plan_mode: ExecutionMode; resources: list[str]; workload: Workload
    provenance: Provenance; load_seconds: float | None = None; ttft_seconds: float | None = None
    prefill_tps: float | None = None; decode_tps: float | None = None
    residency_mib: dict[str, float] = Field(default_factory=dict); utilization: dict[str, float] = Field(default_factory=dict)
    network: dict[str, float | str] = Field(default_factory=dict); power_watts: float | None = None
    error: str | None = None; schema_version: int = 1
    runtime: RuntimeIdentity | None = None  # unbound historical records remain readable

class BenchmarkStore:
    def __init__(self, path: str | Path): self.path = Path(path)
    def append(self, observation: BenchmarkObservation) -> BenchmarkObservation:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f: f.write(observation.model_dump_json() + "\n")
        return observation
    def query(self, profile_id: str | None = None, mode: ExecutionMode | None = None, resources: set[str] | None = None) -> list[BenchmarkObservation]:
        """Retrieve history; these legacy filters do not certify comparability."""
        values = [] if not self.path.exists() else [BenchmarkObservation.model_validate_json(line) for line in self.path.read_text().splitlines() if line]
        return [x for x in values if (profile_id is None or x.profile.id == profile_id)
                and (mode is None or x.plan_mode == mode)
                and (resources is None or resources <= set(x.resources))]

    def query_compatible(self, profile: InferenceProfile, runtime: RuntimeIdentity, workload: Workload, not_before: datetime, mode: ExecutionMode | None = None) -> list[BenchmarkObservation]:
        """Retrieve bound successful measurements; repetition policy is a consumer gate."""
        return [x for x in self.query(mode=mode) if
                x.profile.identity == profile.identity and x.runtime == runtime and x.workload == workload
                and x.resources == profile.participating_resources
                and x.provenance == "measured" and x.error is None
                and evidence_is_fresh(x.timestamp, not_before)]


def evidence_is_fresh(timestamp: datetime, not_before: datetime) -> bool:
    return (timestamp.tzinfo is not None and not_before.tzinfo is not None
            and not_before <= timestamp <= datetime.now(UTC))
