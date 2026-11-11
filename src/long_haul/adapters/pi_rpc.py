"""Minimal translation between Long-Haul work semantics and Pi events."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..work import (
    AttemptStep,
    ProgressKind,
    StepOutcome,
    TaskDigest,
    WorkContract,
    WorkSession,
)
from ..work.repository_context import (
    ContextAdmission,
    authorize_repository_context_delivery,
    render_repository_context,
    render_repository_work,
)
from ..work.repository_models import RepositoryContextView

PI_PACKAGE_NAME = "@earendil-works/pi-coding-agent"
PI_PACKAGE_VERSION = "1.5.2"
PI_UPSTREAM_REPOSITORY = "https://github.com/earendil-works/pi"
PI_UPSTREAM_REVISION = "b4afdddf44e059c67ccf5097e5317d913521b470"
PI_SOURCE_ARCHIVE_SHA256 = (
    "da14b72e5d674959b64fc541e4a9241ac9fcdfa243431479119d3edb598b2231"
)


def pi_tools_for_contract(contract: WorkContract) -> list[str]:
    """Map Long-Haul capabilities onto Pi's native tool allowlist."""
    capabilities = set(contract.capabilities)
    tools: list[str] = []
    if "read" in capabilities:
        tools.append("read")
    if "test" in capabilities or "shell" in capabilities:
        tools.append("bash")
    if "edit" in capabilities:
        tools.extend(["edit", "write"])
    return tools


def pi_prompt(contract: WorkContract, digest: TaskDigest | None = None,
              repository_context: RepositoryContextView | None = None,
              session: WorkSession | None = None,
              context_admissions: dict[str, ContextAdmission] | None = None,
              evaluation_time: datetime | None = None) -> str:
    parts = [contract.objective.strip()]
    if contract.context.strip():
        parts.append(contract.context.strip())
    if contract.repository_work is not None:
        parts.append(render_repository_work(contract.repository_work))
    if digest is not None:
        if digest.contract_id != contract.contract_id:
            raise ValueError("digest belongs to a different work contract")
        parts.append(digest.text)
    if repository_context is not None:
        if contract.repository_work is None:
            raise ValueError("repository context requires a repository work extension")
        repository_context.manifest.assert_bound(contract.repository_work)
        use = repository_context.manifest.selection.use
        if (session is None or session.session_id != use.caller_id or session.crew_id != use.crew_id
                or session.contract_id != contract.contract_id or use.contract_id != contract.contract_id
                or use.mission_id != contract.mission_id):
            raise ValueError("repository context use is not bound to this contract/session")
        if context_admissions is None or evaluation_time is None:
            raise ValueError("repository context delivery requires fresh policy admission")
        authorize_repository_context_delivery(repository_context, context_admissions, evaluation_time)
        parts.append(render_repository_context(repository_context))
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
