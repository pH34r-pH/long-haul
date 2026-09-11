"""Machine-comparable L0-L10 reference-run reports."""
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class LayerState(str, Enum):
    PASS="PASS"; UNSUPPORTED="UNSUPPORTED"; UNKNOWN="UNKNOWN"
    FAIL_SYSTEM="FAIL_SYSTEM"; FAIL_RUNTIME="FAIL_RUNTIME"; FAIL_ARTIFACT="FAIL_ARTIFACT"; FAIL_RESOURCE="FAIL_RESOURCE"
class LayerResult(BaseModel):
    layer: int; state: LayerState; detail: str
class ReferenceReport(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    long_haul_commit: str | None = None; environment: dict[str, object] = Field(default_factory=dict)
    layers: list[LayerResult] = Field(default_factory=list); artifact: dict[str, object] = Field(default_factory=dict)
    def record(self, layer: int, state: LayerState, detail: str) -> None: self.layers.append(LayerResult(layer=layer,state=state,detail=detail))
    def save(self, path: str | Path) -> None:
        path=Path(path); path.parent.mkdir(parents=True,exist_ok=True); path.write_text(self.model_dump_json(indent=2))
