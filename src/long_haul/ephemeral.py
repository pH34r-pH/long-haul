from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class VesselLifecycle(str, Enum): PERSISTENT="persistent"; EPHEMERAL="ephemeral"; EXTERNAL="external"
class ResourceLease(BaseModel):
    id: str; acquired_at: datetime = Field(default_factory=lambda: datetime.now(UTC)); expires_at: datetime | None = None
    preemptible: bool = True; renewable: bool = False; trust_domain: str = "external"; durable_state: bool = False
    def active(self, now: datetime | None = None) -> bool: return self.expires_at is None or (now or datetime.now(UTC)) < self.expires_at
