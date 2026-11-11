"""Authenticated Pi/Nacre launch projection for Long-Haul work sessions.

Long-Haul writes one owner-only context file for a Pi process. The model cannot
select caller identity, memory scope, state root, or resource-policy evidence
through prompt or tool arguments.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from ..resource_policy import (
    RESOURCE_POLICY_PROFILE_ID,
    PolicyDecision,
    ResourceAction,
    ResourceUseRequest,
)
from ..work import WorkContract, WorkSession

PI_NACRE_CONTEXT_SCHEMA_VERSION = 1
PI_NACRE_CONTEXT_MODE = "long-haul-authenticated"


class PiNacreScope(BaseModel):
    scope_type: str = Field(min_length=1)
    scope_id: str = Field(min_length=1)


class PiNacrePolicyReceipt(BaseModel):
    resource_id: str = Field(min_length=1)
    profile_id: Literal[RESOURCE_POLICY_PROFILE_ID] = RESOURCE_POLICY_PROFILE_ID
    policy_id: str = Field(min_length=1)
    policy_fingerprint: str = Field(min_length=1)
    action: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    status: str = Field(min_length=1)
    evaluated_at: str = Field(min_length=1)
    caller_id: str | None = None
    crew_id: str | None = None
    mission_id: str | None = None
    contract_id: str | None = None
    reason: str = ""


class PiNacreCallerContext(BaseModel):
    mode: Literal[PI_NACRE_CONTEXT_MODE] = PI_NACRE_CONTEXT_MODE
    caller_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    crew_id: str = Field(min_length=1)
    mission_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    repository_id: str | None = None
    evidence_ref: str = Field(min_length=1)


class PiNacreLaunchContext(BaseModel):
    caller_context: PiNacreCallerContext
    scopes: list[PiNacreScope] = Field(min_length=1)
    purpose: str = Field(min_length=1)
    policy_decisions: list[PiNacrePolicyReceipt] = Field(default_factory=list)
    source_event_refs: list[str] = Field(default_factory=list)
    capture_proposals: bool = False
    schema_version: Literal[1] = PI_NACRE_CONTEXT_SCHEMA_VERSION

    @model_validator(mode="after")
    def unique_scopes_and_events(self) -> PiNacreLaunchContext:
        scope_keys = {(scope.scope_type, scope.scope_id) for scope in self.scopes}
        if len(scope_keys) != len(self.scopes):
            raise ValueError("Nacre scopes must be unique")
        if len(set(self.source_event_refs)) != len(self.source_event_refs):
            raise ValueError("source_event_refs must be unique")
        return self


def nacre_policy_receipt(
    request: ResourceUseRequest,
    decision: PolicyDecision,
) -> PiNacrePolicyReceipt:
    """Bind one resource-policy decision to the request that produced it."""
    if request.request_id != decision.request_id:
        raise ValueError("resource-policy request and decision ids do not match")
    if request.resource_id != decision.resource_id:
        raise ValueError("resource-policy request and decision resources do not match")
    if request.action is not ResourceAction.AI_AUGMENT:
        raise ValueError("Nacre recall requires an AI_AUGMENT resource request")
    if not request.purpose:
        raise ValueError("Nacre recall requires an explicit purpose")
    if not decision.policy_id or not decision.policy_fingerprint:
        raise ValueError("resource-policy decision lacks policy identity")
    return PiNacrePolicyReceipt(
        resource_id=request.resource_id,
        policy_id=decision.policy_id,
        policy_fingerprint=decision.policy_fingerprint,
        action=request.action.value,
        purpose=request.purpose,
        status=decision.status.value,
        evaluated_at=decision.evaluated_at.isoformat(),
        caller_id=request.caller_id,
        crew_id=request.crew_id,
        mission_id=request.mission_id,
        contract_id=request.contract_id,
        reason=decision.reason,
    )


def build_pi_nacre_context(
    *,
    session: WorkSession,
    contract: WorkContract,
    profile_id: str,
    evidence_ref: str,
    scopes: list[PiNacreScope],
    purpose: str = "mission-assistance",
    repository_id: str | None = None,
    policy_decisions: list[PiNacrePolicyReceipt] | None = None,
    source_event_refs: list[str] | None = None,
    capture_proposals: bool = False,
) -> PiNacreLaunchContext:
    """Build the only model-independent identity projection Pi may pass to Nacre."""
    if session.contract_id != contract.contract_id:
        raise ValueError("Pi session and WorkContract ids do not match")
    if not session.crew_id:
        raise ValueError("authenticated Pi/Nacre launch requires a crew id")
    return PiNacreLaunchContext(
        caller_context=PiNacreCallerContext(
            caller_id=session.session_id,
            profile_id=profile_id,
            session_id=session.session_id,
            crew_id=session.crew_id,
            mission_id=contract.mission_id,
            contract_id=contract.contract_id,
            repository_id=repository_id,
            evidence_ref=evidence_ref,
        ),
        scopes=scopes,
        purpose=purpose,
        policy_decisions=policy_decisions or [],
        source_event_refs=source_event_refs or [],
        capture_proposals=capture_proposals,
    )


def _assert_safe_output_path(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    if not path.is_absolute():
        raise ValueError("Pi/Nacre context path must be absolute")
    if absolute.exists() and absolute.is_symlink():
        raise PermissionError("Pi/Nacre context path cannot be a symlink")
    parent = absolute.parent
    if parent.exists() and parent.is_symlink():
        raise PermissionError("Pi/Nacre context parent cannot be a symlink")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        parent.chmod(0o700)
    return absolute


def write_pi_nacre_context(path: Path, context: PiNacreLaunchContext) -> Path:
    """Atomically write an owner-only context document for one Pi process."""
    target = _assert_safe_output_path(path)
    payload = (
        json.dumps(
            context.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if os.name == "posix":
            target.chmod(0o600)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def pi_nacre_environment(
    *,
    context_file: Path,
    state_root: Path,
    python_executable: Path,
    module_name: str = "nacre_recall",
) -> dict[str, str]:
    """Return the explicit environment projection for the Pi extension."""
    paths = (context_file, state_root, python_executable)
    if any(not path.is_absolute() for path in paths):
        raise ValueError("Pi/Nacre environment paths must be absolute")
    return {
        "NACRE_RECALL_CALLER_CONTEXT_FILE": str(context_file),
        "NACRE_RECALL_STATE_ROOT": str(state_root),
        "NACRE_RECALL_PYTHON": str(python_executable),
        "NACRE_RECALL_MODULE": module_name,
    }
