from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from ..models import ExecutionMode, InferenceProfile

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

class BenchmarkStore:
    def __init__(self, path: str | Path): self.path = Path(path)
    def append(self, observation: BenchmarkObservation) -> BenchmarkObservation:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f: f.write(observation.model_dump_json() + "\n")
        return observation
    def query(self, profile_id: str | None = None, mode: ExecutionMode | None = None, resources: set[str] | None = None) -> list[BenchmarkObservation]:
        values = [] if not self.path.exists() else [BenchmarkObservation.model_validate_json(l) for l in self.path.read_text().splitlines() if l]
        return [x for x in values if (profile_id is None or x.profile.id == profile_id) and (mode is None or x.plan_mode == mode) and (resources is None or resources <= set(x.resources))]
