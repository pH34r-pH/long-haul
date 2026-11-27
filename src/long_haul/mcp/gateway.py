from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from .identity import AuthenticatedPrincipal


class CapabilityStatus(StrEnum):
    OK = "OK"
    DENIED = "DENIED"
    UNSUPPORTED = "UNSUPPORTED"


class CapabilityReason(StrEnum):
    AUTHENTICATED_CONTEXT_REQUIRED = "authenticated-context-required"
    POLICY_UNAVAILABLE = "capability-policy-unavailable"
    UNSUPPORTED = "unsupported-capability"


@dataclass(frozen=True)
class CapabilityResult:
    status: CapabilityStatus
    capability: str
    data: dict[str, Any]
    reason: str | None = None
    reason_code: CapabilityReason | None = None
    schema_version: int = field(default=1, init=False)


class ReferenceVesselAdapter(Protocol):
    def vessel_status(self) -> dict[str, Any]: ...
    def execution_start(self) -> dict[str, Any]: ...
    def execution_status(self) -> dict[str, Any]: ...
    def execution_log(self) -> dict[str, Any]: ...
    def execution_report(self) -> dict[str, Any]: ...
    def execution_stop(self) -> dict[str, Any]: ...


class CapabilityGateway:
    """Capability catalog with dispatch disabled until trusted policy is available."""

    _operations: ClassVar[tuple[str, ...]] = (
        "vessel.status", "execution.start", "execution.status", "execution.log", "execution.report", "execution.stop",
    )

    def __init__(self, adapter: ReferenceVesselAdapter) -> None:
        self._adapter = adapter

    @property
    def capabilities(self) -> tuple[str, ...]:
        return self._operations

    def inspect(self, *, principal: AuthenticatedPrincipal | None = None) -> list[dict[str, str | bool]]:
        reason = self._denial_reason(principal)
        return [
            {
                "capability": capability,
                "discoverable": True,
                "authorized": False,
                "executable": False,
                "authorization": reason.value,
            }
            for capability in self.capabilities
        ]

    def invoke(self, capability: str, *, principal: AuthenticatedPrincipal | None = None) -> CapabilityResult:
        if capability not in self._operations:
            return CapabilityResult(
                status=CapabilityStatus.UNSUPPORTED,
                capability=capability,
                data={},
                reason="Capability is not part of the first Long Haul MCP surface.",
                reason_code=CapabilityReason.UNSUPPORTED,
            )
        return CapabilityResult(
            status=CapabilityStatus.DENIED,
            capability=capability,
            data={},
            reason="Invocation requires authenticated caller context and an admitted capability policy.",
            reason_code=self._denial_reason(principal),
        )

    @staticmethod
    def _denial_reason(principal: AuthenticatedPrincipal | None) -> CapabilityReason:
        if isinstance(principal, AuthenticatedPrincipal):
            return CapabilityReason.POLICY_UNAVAILABLE
        return CapabilityReason.AUTHENTICATED_CONTEXT_REQUIRED
