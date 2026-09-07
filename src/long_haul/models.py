from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ExecutionMode(str, Enum):
    LOCAL = "LOCAL"
    POOL = "POOL"
    PIPELINE = "PIPELINE"
    COMPOSE = "COMPOSE"


class Position(str, Enum):
    SUPPORT = "support"
    CONSENT = "consent"
    STAND_ASIDE = "stand_aside"
    CONCERN = "concern"
    OBJECT = "object"
    BLOCK = "block"
    ABSTAIN = "abstain"
    UNKNOWN = "unknown"


class Resource(BaseModel):
    id: str
    kind: Literal["cpu", "gpu", "npu", "memory", "sensor"]
    model: str | None = None
    architecture: str | None = None
    memory_mb: int | None = None
    notes: str | None = None


class Link(BaseModel):
    source: str
    target: str
    kind: Literal["pcie", "memory", "tailscale", "ethernet", "other"]
    direct: bool | None = None
    latency_ms: float | None = None
    read_gbps: float | None = None
    write_gbps: float | None = None
    throughput_mbps: float | None = None
    notes: str | None = None


class Vessel(BaseModel):
    id: str
    name: str
    class_name: Literal["station", "ship", "light_craft"]
    tailscale_name: str | None = None
    resources: list[Resource] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)


class Competence(BaseModel):
    domain: str
    sample_count: int = 0
    success_rate: float | None = None
    calibration_error: float | None = None


class Embodiment(BaseModel):
    model: str
    vessel: str
    resource: str | None = None
    runtime: str | None = None


class CrewMember(BaseModel):
    crew_id: str
    callsign: str
    primary_role: str
    scopes: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    embodiment: Embodiment | None = None
    competence: list[Competence] = Field(default_factory=list)
    generation: int = 1


class Claim(BaseModel):
    speaker: str
    type: Literal["observation", "self_report", "inference", "unknown", "request"]
    content: str
    confidence: float | None = None
    evidence: list[str] = Field(default_factory=list)


class DecisionPosition(BaseModel):
    participant: str
    position: Position
    category: Literal["factual", "safety", "resource", "value", "jurisdiction", "preference", "other"] | None = None
    rationale: str | None = None
    evidence: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    decision_id: str
    proposal: str
    positions: list[DecisionPosition] = Field(default_factory=list)
    outcome: Literal["consensus", "consent", "scoped_authority", "experiment", "escalation", "recorded_disagreement"] | None = None
    action: str | None = None


class ExecutionPlan(BaseModel):
    plan_id: str
    mode: ExecutionMode
    crew: list[str] = Field(default_factory=list)
    vessels: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    estimated_value: float | None = None
    notes: str | None = None
