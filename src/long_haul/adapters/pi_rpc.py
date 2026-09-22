"""Minimal translation between Long Haul work semantics and upstream Pi RPC events."""
from __future__ import annotations

from typing import Any

from ..work import AttemptStep, ProgressKind, StepOutcome, TaskDigest, WorkContract

PI_PACKAGE_VERSION = "0.87.0"
PI_UPSTREAM_REVISION = "e40126f578ccc0a4a21f8d30cd8e96c8ecfe1722"


def pi_tools_for_contract(contract: WorkContract) -> list[str]:
    """Map Long Haul capabilities onto Pi's native tool allowlist."""
    capabilities = set(contract.capabilities)
    tools: list[str] = []
    if "read" in capabilities:
        tools.append("read")
    if "test" in capabilities or "shell" in capabilities:
        tools.append("bash")
    if "edit" in capabilities:
        tools.extend(["edit", "write"])
    return tools


def pi_prompt(contract: WorkContract, digest: TaskDigest | None = None) -> str:
    parts = [contract.objective.strip()]
    if contract.context.strip():
        parts.append(contract.context.strip())
    if digest is not None:
        if digest.contract_id != contract.contract_id:
            raise ValueError("digest belongs to a different work contract")
        parts.append(digest.text)
    return "\n\n".join(parts)


def attempt_step_from_pi_event(event: dict[str, Any], step_id: str) -> AttemptStep | None:
    """Translate only authoritative upstream tool-completion events."""
    if event.get("type") != "tool_execution_end":
        return None
    action = str(event.get("toolName", "unknown"))
    is_error = bool(event.get("isError", False))
    if action in {"edit", "write"}:
        progress = ProgressKind.STATE_CHANGE
    elif action == "bash":
        progress = ProgressKind.VERIFICATION
    else:
        progress = ProgressKind.INFORMATION
    result = event.get("result")
    result_fingerprint = None
    if isinstance(result, dict):
        details = result.get("details")
        if isinstance(details, dict):
            fingerprint = details.get("fingerprint")
            if fingerprint is not None:
                result_fingerprint = str(fingerprint)
    return AttemptStep(
        step_id=step_id,
        action=action,
        arguments={},
        outcome=StepOutcome.FAILURE if is_error else StepOutcome.SUCCESS,
        progress=progress,
        result_fingerprint=result_fingerprint,
    )
