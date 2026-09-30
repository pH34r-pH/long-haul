"""Vendor-neutral domain models shared by Long Haul subsystems."""
from __future__ import annotations

import hashlib
import json
import math
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    """Type-sensitive identity: sort object keys, retain array order and JSON numbers."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

class ExecutionMode(str, Enum):
    LOCAL = "LOCAL"; POOL = "POOL"; PIPELINE = "PIPELINE"; COMPOSE = "COMPOSE"

class Position(str, Enum):
    SUPPORT = "support"; CONSENT = "consent"; STAND_ASIDE = "stand_aside"; CONCERN = "concern"
    OBJECT = "object"; BLOCK = "block"; ABSTAIN = "abstain"; UNKNOWN = "unknown"

class ResourceKind(str, Enum):
    CPU = "cpu"; GPU = "gpu"; NPU = "npu"; MEMORY = "memory"; STORAGE = "storage"; SENSOR = "sensor"; OTHER = "other"

class Capacity(BaseModel):
    """A typed amount; capacities of unlike kinds must never be summed implicitly."""
    kind: Literal["memory", "storage", "compute", "power"]
    amount: float = Field(ge=0, allow_inf_nan=False)
    unit: Literal["MiB", "GiB", "bytes", "TOPS", "watts"]

    @model_validator(mode="after")
    def compatible_unit(self) -> Capacity:
        units = {"memory": {"MiB", "GiB", "bytes"}, "storage": {"MiB", "GiB", "bytes"}, "compute": {"TOPS"}, "power": {"watts"}}
        if self.unit not in units[self.kind]:
            raise ValueError(f"incompatible capacity kind/unit: {self.kind}/{self.unit}")
        if self.kind in {"memory", "storage"}:
            self.as_mib()
        return self

    def as_mib(self) -> float:
        if self.kind not in {"memory", "storage"}:
            raise ValueError("only memory/storage capacities convert to MiB")
        converted = self.amount * {"MiB": 1, "GiB": 1024, "bytes": 1 / (1024 * 1024)}[self.unit]
        if not math.isfinite(converted):
            raise ValueError("capacity conversion exceeds finite MiB range")
        return converted

class Resource(BaseModel):
    id: str; kind: ResourceKind; model: str | None = None; architecture: str | None = None
    capacities: list[Capacity] = Field(default_factory=list)
    memory_mb: int | None = Field(default=None, ge=0)  # legacy manifest compatibility
    notes: str | None = None; metadata: dict[str, Any] = Field(default_factory=dict)
    @model_validator(mode="after")
    def migrate_memory_mb(self) -> Resource:
        if self.memory_mb is not None and not any(c.kind == "memory" for c in self.capacities):
            self.capacities.append(Capacity(kind="memory", amount=self.memory_mb, unit="MiB"))
        if len({c.kind for c in self.capacities}) != len(self.capacities):
            raise ValueError("resource capacity kinds must be unique; capacities are not additive")
        return self
    def capacity_mib(self, kind: Literal["memory", "storage"] = "memory") -> float | None:
        for capacity in self.capacities:
            if capacity.kind != kind:
                continue
            return capacity.as_mib()
        return None

class Link(BaseModel):
    source: str; target: str
    kind: Literal["pcie", "memory", "tailscale", "ethernet", "storage_bus", "other"]
    direct: bool | None = None; latency_ms: float | None = Field(default=None, ge=0)
    read_gbps: float | None = Field(default=None, ge=0); write_gbps: float | None = Field(default=None, ge=0)
    throughput_mbps: float | None = Field(default=None, ge=0); path: str | None = None; measured: bool = False; notes: str | None = None

class Vessel(BaseModel):
    id: str; name: str; class_name: Literal["station", "ship", "light_craft"]; tailscale_name: str | None = None
    resources: list[Resource] = Field(default_factory=list); links: list[Link] = Field(default_factory=list)
    @model_validator(mode="after")
    def resource_references_exist(self) -> Vessel:
        ids = {r.id for r in self.resources}
        if len(ids) != len(self.resources): raise ValueError("resource ids must be unique within a vessel")
        for link in self.links:
            if link.source not in ids or link.target not in ids: raise ValueError("links must reference vessel resources")
        return self

class ModelArtifact(BaseModel):
    """Implementation input, never a crew identity."""
    foundation: str; revision: str | None = None; quantization: str | None = None
    adapters: list[str] = Field(default_factory=list); auxiliary_artifacts: list[str] = Field(default_factory=list)
    @property
    def key(self) -> str:
        # Versioned full artifact identity. Historical keys remain readable in
        # validation records but cannot certify a different current artifact.
        encoded = canonical_json(self.model_dump())
        return "artifact-v2:" + hashlib.sha256(encoded.encode()).hexdigest()


class InferenceProfile(BaseModel):
    """Runtime strategy is deliberately orthogonal to execution mode."""
    id: str; runtime_id: str; strategy: str; artifact: ModelArtifact
    participating_resources: list[str] = Field(min_length=1); requirements: list[Capacity] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict); schema_version: int = SCHEMA_VERSION

    @property
    def identity(self) -> str:
        return canonical_json(self.model_dump())

    @model_validator(mode="after")
    def unambiguous_options(self) -> InferenceProfile:
        canonical_json(self.options)
        return self

class Competence(BaseModel):
    domain: str; sample_count: int = 0; success_rate: float | None = None; calibration_error: float | None = None

class Embodiment(BaseModel):
    artifact: ModelArtifact | None = None; model: str | None = None  # legacy `model`
    vessel: str; resource: str | None = None; runtime: str | None = None; inference_profile_id: str | None = None
    @model_validator(mode="after")
    def migrate_model(self) -> Embodiment:
        if self.artifact is None and self.model: self.artifact = ModelArtifact(foundation=self.model)
        if self.artifact is None: raise ValueError("embodiment needs artifact or legacy model")
        return self

class CrewMember(BaseModel):
    crew_id: str; callsign: str; primary_role: str; scopes: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list); embodiment: Embodiment | None = None
    competence: list[Competence] = Field(default_factory=list); generation: int = 1

class Claim(BaseModel):
    speaker: str; type: Literal["observation", "self_report", "inference", "unknown", "request"]
    content: str; confidence: float | None = Field(default=None, ge=0, le=1); evidence: list[str] = Field(default_factory=list)

class DecisionPosition(BaseModel):
    participant: str; position: Position
    category: Literal["factual", "safety", "resource", "value", "jurisdiction", "preference", "other"] | None = None
    rationale: str | None = None; evidence: list[str] = Field(default_factory=list)

class Decision(BaseModel):
    decision_id: str; proposal: str; positions: list[DecisionPosition] = Field(default_factory=list)
    outcome: Literal["consensus", "consent", "scoped_authority", "experiment", "escalation", "recorded_disagreement"] | None = None; action: str | None = None

class ExecutionPlan(BaseModel):
    plan_id: str; mode: ExecutionMode; crew: list[str] = Field(default_factory=list)
    vessels: list[str] = Field(default_factory=list); resources: list[str] = Field(default_factory=list)
    inference_profile_id: str | None = None; estimated_value: float | None = None; notes: str | None = None
