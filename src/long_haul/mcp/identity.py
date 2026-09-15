from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """Transport-authenticated identity presented to the capability plane.

    Provider-specific claims are retained for provenance, but callers cannot
    select their Long Haul identity through MCP tool arguments. #17 will map
    this authenticated subject onto authoritative crew/session identity.
    """

    issuer: str
    subject: str
    tenant_id: str | None = None
    name: str | None = None
    client_id: str | None = None
    claims: dict[str, str] = field(default_factory=dict)

    def public_view(self) -> dict[str, str | None]:
        return {
            "issuer": self.issuer,
            "subject": self.subject,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "client_id": self.client_id,
        }
