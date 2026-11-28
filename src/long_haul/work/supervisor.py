"""Out-of-band, fail-closed micro-supervisor contract for bounded local triage (#334).

The micro-supervisor runs strictly in the trusted control plane outside worker sandboxes
and transcripts. It consumes only deterministically lifted host telemetry and emits a
bounded 3-field proposal wrapped in a deterministic safety gate that fails closed to
``HALT_FOR_HUMAN`` on any parse error, reasoning-tag leak, or policy violation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..decisions.engine import DecisionEngine, DecisionRecord
from ..events.store import Event
from ..models import InferenceProfile
from ..resource_policy_models import canonical_json
from ..runtime import ExecutionRequest, RuntimeAdapter
from .checkpoints import TaskDigest, WorkCheckpoint
from .progress import ProgressIntervention
from .repository_models import content_digest

SUPERVISOR_SCHEMA_VERSION = 1
SUPERVISOR_MAX_TOKENS = 64
SUPERVISOR_MAX_DIAGNOSIS_CHARS = 180
SUPERVISOR_MAX_TARGET_CHARS = 128
SUPERVISOR_SYSTEM_PROMPT = (
    "Control-plane micro-supervisor. Output ONLY a JSON object with keys "
    '"diagnosis", "action", and "target" (no <think>, no markdown).'
)

_THINK_TAG_RE = re.compile(r"</?think\b", re.IGNORECASE)
_LINE_DIAG_RE = re.compile(r"^DIAGNOSIS:\s*(.+)$", re.MULTILINE)
_LINE_ACT_RE = re.compile(r"^ACTION:\s*(\S+)\s*$", re.MULTILINE)
_LINE_TARGET_RE = re.compile(r"^TARGET:\s*(.+)$", re.MULTILINE)


class SupervisorAction(str, Enum):
    RETRY_CONSTRAINED = "RETRY_CONSTRAINED"
    ESCALATE_EMBODIMENT = "ESCALATE_EMBODIMENT"
    HALT_FOR_HUMAN = "HALT_FOR_HUMAN"


class SupervisorRejectionReason(str, Enum):
    THINK_TAG_LEAK = "think_tag_leak"
    PARSE_ERROR = "parse_error"
    SCHEMA_VIOLATION = "schema_violation"
    DIAGNOSIS_TOO_LONG = "diagnosis_too_long"
    OBSERVATION_INCOMPLETE = "observation_incomplete"
    SCOPE_VIOLATION = "scope_violation"
    LEASE_INVALID = "lease_invalid"
    CLEANUP_REQUIRED = "cleanup_required"
    ACTIVE_OBJECTION_BLOCK = "active_objection_block"
    RETRY_BUDGET_EXHAUSTED = "retry_budget_exhausted"
    TARGET_NOT_ADMITTED = "target_not_admitted"
    TARGET_MISSING = "target_missing"
    UNEXPECTED_TARGET = "unexpected_target"
    RUNTIME_EXECUTION_FAILED = "runtime_execution_failed"


HAZARD_REJECTION_REASONS: frozenset[SupervisorRejectionReason] = frozenset(
    {
        SupervisorRejectionReason.OBSERVATION_INCOMPLETE,
        SupervisorRejectionReason.SCOPE_VIOLATION,
        SupervisorRejectionReason.LEASE_INVALID,
        SupervisorRejectionReason.CLEANUP_REQUIRED,
        SupervisorRejectionReason.ACTIVE_OBJECTION_BLOCK,
        SupervisorRejectionReason.RETRY_BUDGET_EXHAUSTED,
    }
)


class SupervisorSafetyFlags(BaseModel):
    """Host-computed safety flags; defaults fail closed if omitted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    obs_complete: bool = False
    scope_ok: bool = False
    lease_valid: bool = False
    cleanup_required: bool = True
    active_objection_block: bool = False
    retry_budget_remaining: int = Field(default=0, ge=0)


class SupervisorLiftContext(BaseModel):
    """Optional control-plane artifacts lifted into ``SupervisorTelemetry``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    digest: TaskDigest | None = None
    checkpoint: WorkCheckpoint | None = None
    decision_record: DecisionRecord | None = None
    session_id: str | None = None
    allowed_retry_constraints: tuple[str, ...] = Field(default_factory=tuple)
    allowed_escalation_targets: tuple[str, ...] = Field(default_factory=tuple)


class SupervisorTelemetry(BaseModel):
    """Deterministically lifted control-plane telemetry (never raw worker transcripts)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SUPERVISOR_SCHEMA_VERSION
    contract_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    session_id: str | None = None
    intervention_reason: str = Field(min_length=1)
    action_signature: str = Field(min_length=1)
    recent_count: int = Field(default=0, ge=0)
    consecutive_count: int = Field(default=0, ge=0)
    no_progress_steps: int = Field(default=0, ge=0)
    unresolved_failure_count: int = Field(default=0, ge=0)
    digest_summary: str = Field(default="", max_length=1024)
    digest_truncated: bool = False
    allowed_retry_constraints: tuple[str, ...] = Field(default_factory=tuple)
    allowed_escalation_targets: tuple[str, ...] = Field(default_factory=tuple)
    flags: SupervisorSafetyFlags = Field(default_factory=SupervisorSafetyFlags)

    @property
    def fingerprint(self) -> str:
        return content_digest(canonical_json(self.model_dump(mode="json")).encode())

    def render_prompt(self) -> str:
        retry_list = ", ".join(self.allowed_retry_constraints) if self.allowed_retry_constraints else "NONE"
        esc_list = ", ".join(self.allowed_escalation_targets) if self.allowed_escalation_targets else "NONE"
        summary = (self.digest_summary.strip() if self.flags.obs_complete else "<INCOMPLETE>") or "<EMPTY>"
        f = self.flags
        return (
            "[CONTROL-PLANE TELEMETRY]\n"
            f"CONTRACT: {self.contract_id} ATTEMPT: {self.attempt_id}\n"
            f"INTERVENTION: {self.intervention_reason} SIG: {self.action_signature[:16]}\n"
            f"COUNTS: recent={self.recent_count} consecutive={self.consecutive_count} "
            f"no_progress={self.no_progress_steps} unresolved_failures={self.unresolved_failure_count}\n"
            f"CTX: obs_complete={int(f.obs_complete)} scope_ok={int(f.scope_ok)} "
            f"lease_valid={int(f.lease_valid)} cleanup_required={int(f.cleanup_required)} "
            f"active_objection_block={int(f.active_objection_block)} "
            f"retry_budget_remaining={f.retry_budget_remaining}\n"
            f"ALLOWED_RETRY_CONSTRAINTS: {retry_list}\n"
            f"ALLOWED_ESCALATION_TARGETS: {esc_list}\n"
            f"DIGEST_SUMMARY: {summary}\n"
            'Output ONLY JSON {"diagnosis": "...", "action": "RETRY_CONSTRAINED|ESCALATE_EMBODIMENT|HALT_FOR_HUMAN", '
            '"target": "<admitted id or NONE>"} with no <think> tags and no markdown.'
        )


class SupervisorProposal(BaseModel):
    """Untrusted bounded 3-field proposal emitted by a micro-supervisor embodiment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    diagnosis: str = Field(min_length=1, max_length=SUPERVISOR_MAX_DIAGNOSIS_CHARS)
    action: SupervisorAction
    target: str = Field(default="NONE", min_length=1, max_length=SUPERVISOR_MAX_TARGET_CHARS)


class SupervisorGatePolicy(BaseModel):
    """Deterministic control-plane gate governing proposal admission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_diagnosis_chars: int = Field(default=SUPERVISOR_MAX_DIAGNOSIS_CHARS, ge=16, le=512)
    forbid_think_tags: bool = True
    require_admitted_target: bool = True
    allow_escalation_when_lease_invalid: bool = True
    allow_escalation_when_obs_incomplete: bool = False


class SupervisorRunMetadata(BaseModel):
    """Execution provenance attached to a ``SupervisorDecision``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_fingerprint: str | None = None
    profile_id: str | None = None
    artifact_key: str | None = None
    request_id: str | None = None


class SupervisorRunOptions(BaseModel):
    """Bounded invocation options for running a local micro-supervisor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int = Field(default=0, ge=0, le=2147483647)
    max_tokens: int = Field(default=SUPERVISOR_MAX_TOKENS, ge=1, le=SUPERVISOR_MAX_TOKENS)
    timeout_seconds: float = Field(default=15.0, gt=0.0)
    prompt_mode: Literal["runtime-default", "rendered"] = "rendered"
    rendered_prompt: str | None = None
    policy: SupervisorGatePolicy = Field(default_factory=SupervisorGatePolicy)


class SupervisorDecision(BaseModel):
    """Authoritative fail-closed disposition emitted by the control-plane gate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SUPERVISOR_SCHEMA_VERSION
    telemetry_fingerprint: str = Field(min_length=1)
    proposed_action: SupervisorAction | None = None
    proposed_target: str | None = None
    effective_action: SupervisorAction
    effective_target: str = "NONE"
    diagnosis: str = Field(min_length=1, max_length=512)
    failed_closed: bool = False
    rejection_reason: SupervisorRejectionReason | None = None
    detail: str = ""
    runtime_fingerprint: str | None = None
    profile_id: str | None = None
    artifact_key: str | None = None
    request_id: str | None = None


def supervisor_json_schema(
    max_diagnosis_chars: int = SUPERVISOR_MAX_DIAGNOSIS_CHARS,
    max_target_chars: int = SUPERVISOR_MAX_TARGET_CHARS,
) -> dict[str, object]:
    """Return a strict JSON Schema compatible with ``LlamaServerAdapter`` options."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["diagnosis", "action", "target"],
        "properties": {
            "diagnosis": {"type": "string", "minLength": 1, "maxLength": max_diagnosis_chars},
            "action": {"type": "string", "enum": [action.value for action in SupervisorAction]},
            "target": {"type": "string", "minLength": 1, "maxLength": max_target_chars},
        },
    }


def _dedupe_nonempty(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in items if item.strip()))


def lift_supervisor_telemetry(
    contract_id: str,
    attempt_id: str,
    intervention: ProgressIntervention,
    flags: SupervisorSafetyFlags | None = None,
    context: SupervisorLiftContext | None = None,
) -> SupervisorTelemetry:
    """Deterministically lift control-plane artifacts into ``SupervisorTelemetry``."""
    ctx = context or SupervisorLiftContext()
    if ctx.digest is not None and ctx.digest.contract_id != contract_id:
        raise ValueError("TaskDigest contract_id does not match telemetry contract_id")
    if ctx.checkpoint is not None and ctx.checkpoint.contract_id != contract_id:
        raise ValueError("WorkCheckpoint contract_id does not match telemetry contract_id")

    base_flags = flags or SupervisorSafetyFlags()
    has_block = base_flags.active_objection_block or (
        bool(DecisionEngine().active_blocks(ctx.decision_record)) if ctx.decision_record is not None else False
    )
    resolved_flags = base_flags.model_copy(update={"active_objection_block": has_block})

    return SupervisorTelemetry(
        contract_id=contract_id,
        attempt_id=attempt_id,
        session_id=ctx.session_id,
        intervention_reason=intervention.reason,
        action_signature=intervention.signature,
        recent_count=intervention.recent_count,
        consecutive_count=intervention.consecutive_count,
        no_progress_steps=intervention.no_progress_steps,
        unresolved_failure_count=len(ctx.checkpoint.unresolved_failures) if ctx.checkpoint is not None else 0,
        digest_summary=(ctx.digest.text if ctx.digest is not None else "")[:1024],
        digest_truncated=bool(ctx.digest.truncated) if ctx.digest is not None else False,
        allowed_retry_constraints=_dedupe_nonempty(ctx.allowed_retry_constraints),
        allowed_escalation_targets=_dedupe_nonempty(ctx.allowed_escalation_targets),
        flags=resolved_flags,
    )


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _decode_raw_payload(cleaned: str) -> tuple[dict[str, object] | None, SupervisorRejectionReason | None, str]:
    if cleaned.startswith("{"):
        try:
            decoded = json.loads(cleaned, object_pairs_hook=_unique_json_pairs)
        except (ValueError, RecursionError) as exc:
            return None, SupervisorRejectionReason.PARSE_ERROR, f"invalid JSON payload: {exc}"
        if not isinstance(decoded, dict):
            return None, SupervisorRejectionReason.SCHEMA_VIOLATION, "JSON root must be an object"
        return decoded, None, "ok"

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if len(lines) != 3:
        return None, SupervisorRejectionReason.PARSE_ERROR, "expected JSON object or exact 3-line output"
    diag_m, act_m, tgt_m = _LINE_DIAG_RE.match(lines[0]), _LINE_ACT_RE.match(lines[1]), _LINE_TARGET_RE.match(lines[2])
    if not (diag_m and act_m and tgt_m):
        return None, SupervisorRejectionReason.PARSE_ERROR, "missing required DIAGNOSIS/ACTION/TARGET lines"
    return (
        {"diagnosis": diag_m.group(1).strip(), "action": act_m.group(1).strip(), "target": tgt_m.group(1).strip()},
        None,
        "ok",
    )


def _validate_payload_dict(
    payload: dict[str, object], gate: SupervisorGatePolicy
) -> tuple[SupervisorProposal | None, SupervisorRejectionReason | None, str]:
    required = {"diagnosis", "action", "target"}
    if set(payload.keys()) != required or any(not isinstance(payload[key], str) for key in required):
        return None, SupervisorRejectionReason.SCHEMA_VIOLATION, "proposal requires string keys diagnosis, action, target"

    diagnosis = str(payload["diagnosis"]).strip()
    if len(diagnosis) > gate.max_diagnosis_chars:
        return (
            None,
            SupervisorRejectionReason.DIAGNOSIS_TOO_LONG,
            f"diagnosis length {len(diagnosis)} exceeds {gate.max_diagnosis_chars}",
        )
    try:
        proposal = SupervisorProposal(
            diagnosis=diagnosis,
            action=SupervisorAction(str(payload["action"]).strip()),
            target=str(payload["target"]).strip(),
        )
    except (ValueError, ValidationError) as exc:
        return None, SupervisorRejectionReason.SCHEMA_VIOLATION, f"proposal schema validation failed: {exc}"
    return proposal, None, "ok"


def parse_supervisor_output(
    raw_text: str,
    policy: SupervisorGatePolicy | None = None,
) -> tuple[SupervisorProposal | None, SupervisorRejectionReason | None, str]:
    """Parse strict JSON or 3-line supervisor output and reject ``<think>`` leaks."""
    gate = policy or SupervisorGatePolicy()
    if not isinstance(raw_text, str) or not raw_text.strip():
        return None, SupervisorRejectionReason.PARSE_ERROR, "empty supervisor output"
    if gate.forbid_think_tags and _THINK_TAG_RE.search(raw_text):
        return None, SupervisorRejectionReason.THINK_TAG_LEAK, "<think> tag leaked into supervisor output"

    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        return None, SupervisorRejectionReason.PARSE_ERROR, "markdown code fences are disallowed"

    payload, reason, detail = _decode_raw_payload(cleaned)
    if payload is None or reason is not None:
        return None, reason or SupervisorRejectionReason.PARSE_ERROR, detail
    return _validate_payload_dict(payload, gate)


@dataclass(frozen=True)
class _DecisionOutcome:
    effective_action: SupervisorAction
    effective_target: str
    failed_closed: bool
    rejection_reason: SupervisorRejectionReason | None = None
    detail: str = ""


def _make_decision(
    telemetry: SupervisorTelemetry,
    proposal: SupervisorProposal | None,
    meta: SupervisorRunMetadata,
    outcome: _DecisionOutcome,
) -> SupervisorDecision:
    reason_label = outcome.rejection_reason.value if outcome.rejection_reason else "rejected"
    diag = proposal.diagnosis if proposal is not None else f"Fail-closed halt: {reason_label}"
    return SupervisorDecision(
        telemetry_fingerprint=telemetry.fingerprint,
        proposed_action=proposal.action if proposal is not None else None,
        proposed_target=proposal.target if proposal is not None else None,
        effective_action=outcome.effective_action,
        effective_target=outcome.effective_target,
        diagnosis=diag[:512],
        failed_closed=outcome.failed_closed,
        rejection_reason=outcome.rejection_reason,
        detail=outcome.detail,
        runtime_fingerprint=meta.runtime_fingerprint,
        profile_id=meta.profile_id,
        artifact_key=meta.artifact_key,
        request_id=meta.request_id,
    )


def _first_rejection(
    checks: tuple[tuple[bool, SupervisorRejectionReason, str], ...],
) -> tuple[SupervisorRejectionReason, str] | None:
    for triggered, reason, detail in checks:
        if triggered:
            return reason, detail
    return None


def _check_proposal_policy(
    telemetry: SupervisorTelemetry,
    proposal: SupervisorProposal,
    gate: SupervisorGatePolicy,
) -> tuple[SupervisorRejectionReason, str] | None:
    flags = telemetry.flags
    if proposal.action is SupervisorAction.HALT_FOR_HUMAN:
        if proposal.target != "NONE":
            return SupervisorRejectionReason.UNEXPECTED_TARGET, f"HALT_FOR_HUMAN requires target 'NONE', got {proposal.target!r}"
        return None

    if proposal.action is SupervisorAction.RETRY_CONSTRAINED:
        checks = (
            (not flags.obs_complete, SupervisorRejectionReason.OBSERVATION_INCOMPLETE, "cannot retry when observation is incomplete"),
            (not flags.scope_ok, SupervisorRejectionReason.SCOPE_VIOLATION, "cannot retry after scope violation"),
            (flags.cleanup_required, SupervisorRejectionReason.CLEANUP_REQUIRED, "cannot retry while worker cleanup is unconfirmed"),
            (not flags.lease_valid, SupervisorRejectionReason.LEASE_INVALID, "cannot retry on invalid or expired lease"),
            (flags.active_objection_block, SupervisorRejectionReason.ACTIVE_OBJECTION_BLOCK, "cannot retry while an active objection block holds"),
            (flags.retry_budget_remaining <= 0, SupervisorRejectionReason.RETRY_BUDGET_EXHAUSTED, "retry budget exhausted"),
            (proposal.target == "NONE", SupervisorRejectionReason.TARGET_MISSING, "RETRY_CONSTRAINED requires an explicit constraint target"),
            (
                gate.require_admitted_target and proposal.target not in telemetry.allowed_retry_constraints,
                SupervisorRejectionReason.TARGET_NOT_ADMITTED,
                f"retry constraint {proposal.target!r} is not in admitted allowlist",
            ),
        )
        return _first_rejection(checks)

    checks = (
        (not flags.obs_complete and not gate.allow_escalation_when_obs_incomplete, SupervisorRejectionReason.OBSERVATION_INCOMPLETE, "cannot escalate when observation is incomplete"),
        (not flags.scope_ok, SupervisorRejectionReason.SCOPE_VIOLATION, "cannot escalate after scope violation"),
        (flags.cleanup_required, SupervisorRejectionReason.CLEANUP_REQUIRED, "cannot escalate while worker cleanup is unconfirmed"),
        (not flags.lease_valid and not gate.allow_escalation_when_lease_invalid, SupervisorRejectionReason.LEASE_INVALID, "escalation blocked on invalid lease by gate policy"),
        (flags.active_objection_block, SupervisorRejectionReason.ACTIVE_OBJECTION_BLOCK, "cannot escalate while an active objection block holds"),
        (proposal.target == "NONE", SupervisorRejectionReason.TARGET_MISSING, "ESCALATE_EMBODIMENT requires an explicit escalation target"),
        (
            gate.require_admitted_target and proposal.target not in telemetry.allowed_escalation_targets,
            SupervisorRejectionReason.TARGET_NOT_ADMITTED,
            f"escalation target {proposal.target!r} is not in admitted allowlist",
        ),
    )
    return _first_rejection(checks)


def evaluate_supervisor_proposal(
    telemetry: SupervisorTelemetry,
    raw_output: str,
    policy: SupervisorGatePolicy | None = None,
    metadata: SupervisorRunMetadata | None = None,
) -> SupervisorDecision:
    """Validate an untrusted supervisor proposal and fail closed to ``HALT_FOR_HUMAN``."""
    gate = policy or SupervisorGatePolicy()
    meta = metadata or SupervisorRunMetadata()
    proposal, parse_reason, parse_detail = parse_supervisor_output(raw_output, gate)
    if proposal is None or parse_reason is not None:
        return _make_decision(
            telemetry,
            None,
            meta,
            _DecisionOutcome(
                SupervisorAction.HALT_FOR_HUMAN,
                "NONE",
                True,
                parse_reason or SupervisorRejectionReason.PARSE_ERROR,
                parse_detail,
            ),
        )

    rejection = _check_proposal_policy(telemetry, proposal, gate)
    if rejection is not None:
        return _make_decision(
            telemetry,
            proposal,
            meta,
            _DecisionOutcome(SupervisorAction.HALT_FOR_HUMAN, "NONE", True, rejection[0], rejection[1]),
        )

    return _make_decision(
        telemetry,
        proposal,
        meta,
        _DecisionOutcome(proposal.action, proposal.target, False, None, f"accepted {proposal.action.value}"),
    )


def build_supervisor_execution_request(
    telemetry: SupervisorTelemetry,
    profile: InferenceProfile,
    request_id: str,
    options: SupervisorRunOptions | None = None,
) -> ExecutionRequest:
    """Build a hard-bounded ``ExecutionRequest`` compatible with ``LlamaServerAdapter``."""
    opts = options or SupervisorRunOptions()
    context_tokens: int | None = None
    if opts.prompt_mode == "rendered":
        raw_context = profile.options.get("context_size")
        if type(raw_context) is not int or raw_context <= 0:
            raise ValueError("rendered supervisor requests require positive integer profile.options['context_size']")
        context_tokens = raw_context
    prompt = opts.rendered_prompt if opts.rendered_prompt is not None else telemetry.render_prompt()
    return ExecutionRequest(
        request_id=request_id,
        profile=profile,
        prompt=prompt,
        max_tokens=opts.max_tokens,
        temperature=0.0,
        timeout_seconds=opts.timeout_seconds,
        allow_unknown_runtime=False,
        seed=opts.seed,
        context_tokens=context_tokens,
        prompt_mode=opts.prompt_mode,
    )


def run_micro_supervisor(
    telemetry: SupervisorTelemetry,
    adapter: RuntimeAdapter,
    profile: InferenceProfile,
    request_id: str,
    options: SupervisorRunOptions | None = None,
) -> SupervisorDecision:
    """Invoke a local runtime adapter out-of-band and enforce the fail-closed gate."""
    opts = options or SupervisorRunOptions()
    request = build_supervisor_execution_request(telemetry, profile, request_id, opts)
    result = adapter.execute(request)
    meta = SupervisorRunMetadata(
        runtime_fingerprint=result.runtime.fingerprint,
        profile_id=profile.id,
        artifact_key=profile.artifact.key,
        request_id=request_id,
    )
    if not result.success or result.output is None:
        err_kind = result.error_class.value if result.error_class is not None else "unknown"
        return _make_decision(
            telemetry,
            None,
            meta,
            _DecisionOutcome(
                SupervisorAction.HALT_FOR_HUMAN,
                "NONE",
                True,
                SupervisorRejectionReason.RUNTIME_EXECUTION_FAILED,
                f"runtime failed ({err_kind}): {result.error_detail or 'no output'}",
            ),
        )
    return evaluate_supervisor_proposal(telemetry, result.output, opts.policy, meta)


def supervisor_decision_event(
    telemetry: SupervisorTelemetry,
    decision: SupervisorDecision,
    actor: str = "control_plane:micro_supervisor",
    source: str = "work.supervisor",
) -> Event:
    """Construct an immutable EventStore record without raw prompts or transcripts."""
    return Event(
        event_type="work_supervisor_decision",
        actor=actor,
        source=source,
        payload={
            "schema_version": decision.schema_version,
            "contract_id": telemetry.contract_id,
            "attempt_id": telemetry.attempt_id,
            "session_id": telemetry.session_id,
            "intervention_reason": telemetry.intervention_reason,
            "action_signature": telemetry.action_signature,
            "telemetry_fingerprint": decision.telemetry_fingerprint,
            "proposed_action": decision.proposed_action.value if decision.proposed_action is not None else None,
            "proposed_target": decision.proposed_target,
            "effective_action": decision.effective_action.value,
            "effective_target": decision.effective_target,
            "diagnosis": decision.diagnosis,
            "failed_closed": decision.failed_closed,
            "rejection_reason": decision.rejection_reason.value if decision.rejection_reason is not None else None,
            "detail": decision.detail,
            "runtime_fingerprint": decision.runtime_fingerprint,
            "profile_id": decision.profile_id,
            "artifact_key": decision.artifact_key,
            "request_id": decision.request_id,
        },
    )
