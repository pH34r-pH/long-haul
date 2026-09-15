from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CapabilityResult:
    status: str
    capability: str
    data: dict[str, Any]
    reason: str | None = None


class ReferenceVesselAdapter(Protocol):
    def vessel_status(self) -> dict[str, Any]: ...
    def execution_start(self) -> dict[str, Any]: ...
    def execution_status(self) -> dict[str, Any]: ...
    def execution_log(self) -> dict[str, Any]: ...
    def execution_report(self) -> dict[str, Any]: ...
    def execution_stop(self) -> dict[str, Any]: ...


class CapabilityGateway:
    """Provider-neutral first slice of the Long Haul capability plane.

    Authorization is intentionally represented as a hook rather than embedded
    provider conditionals. #16 will supply the contextual policy engine.
    """

    _operations = {
        "vessel.status": "vessel_status",
        "execution.start": "execution_start",
        "execution.status": "execution_status",
        "execution.log": "execution_log",
        "execution.report": "execution_report",
        "execution.stop": "execution_stop",
    }

    def __init__(self, adapter: ReferenceVesselAdapter) -> None:
        self._adapter = adapter

    @property
    def capabilities(self) -> tuple[str, ...]:
        return tuple(self._operations)

    def inspect(self) -> list[dict[str, str | bool]]:
        return [
            {
                "capability": capability,
                "discoverable": True,
                "authorized": True,
                "executable": True,
                "authorization": "bootstrap-operator",
            }
            for capability in self.capabilities
        ]

    def invoke(self, capability: str) -> CapabilityResult:
        method_name = self._operations.get(capability)
        if method_name is None:
            return CapabilityResult(
                status="UNSUPPORTED",
                capability=capability,
                data={},
                reason="Capability is not part of the first Long Haul MCP surface.",
            )
        data = getattr(self._adapter, method_name)()
        return CapabilityResult(status="OK", capability=capability, data=data)
