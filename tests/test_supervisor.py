"""Unit and adversarial tests for the out-of-band micro-supervisor contract (#334)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from long_haul.adapters.llama_server import _completion_payload
from long_haul.decisions.engine import DecisionEngine, DecisionRecord
from long_haul.events.store import EventStore
from long_haul.models import DecisionPosition, InferenceProfile, ModelArtifact, Position
from long_haul.runtime import (
    ExecutionRequest,
    ExecutionResult,
    FailureClass,
    RuntimeIdentity,
)
from long_haul.work.checkpoints import TaskDigest, WorkCheckpoint
from long_haul.work.progress import (
    AttemptStep,
    ProgressKind,
    ProgressMonitor,
    StepOutcome,
)
from long_haul.work.supervisor import (
    SUPERVISOR_MAX_TOKENS,
    SupervisorAction,
    SupervisorGatePolicy,
    SupervisorRejectionReason,
    SupervisorSafetyFlags,
    SupervisorTelemetry,
    build_supervisor_execution_request,
    evaluate_supervisor_proposal,
    lift_supervisor_telemetry,
    parse_supervisor_output,
    run_micro_supervisor,
    supervisor_decision_event,
    supervisor_json_schema,
)


def _sample_profile() -> InferenceProfile:
    return InferenceProfile(
        id="prof-cpu-2b-supervisor",
        artifact=ModelArtifact(foundation="minicpm5-2b", revision="r1", quantization="Q4_K_M"),
        runtime_id="llama.cpp",
        strategy="resident",
        participating_resources=["cpu0"],
        options={
            "transport": "llama-server",
            "server_port": 8081,
            "context_size": 512,
            "json_schema": supervisor_json_schema(),
        },
    )


def _sample_telemetry(**flag_overrides: object) -> SupervisorTelemetry:
    flags_data: dict[str, object] = {
        "obs_complete": True,
        "scope_ok": True,
        "lease_valid": True,
        "cleanup_required": False,
        "active_objection_block": False,
        "retry_budget_remaining": 2,
    }
    flags_data.update(flag_overrides)
    return SupervisorTelemetry(
        contract_id="wc-100",
        attempt_id="att-1",
        session_id="sess-1",
        intervention_reason="repeated_failure_pair",
        action_signature="a" * 64,
        recent_count=3,
        consecutive_count=2,
        no_progress_steps=4,
        unresolved_failure_count=1,
        digest_summary="Unit test failed on edge input; narrow scope suggested.",
        allowed_retry_constraints=("narrow_test_scope", "pin_readonly_check"),
        allowed_escalation_targets=("plan:local-larger", "plan:remote-anchorage"),
        flags=SupervisorSafetyFlags(**flags_data),
    )


class _FakeAdapter:
    runtime_id = "llama.cpp"

    def __init__(self, result: ExecutionResult) -> None:
        self._result = result
        self.last_request: ExecutionRequest | None = None

    def identity(self) -> RuntimeIdentity:
        return self._result.runtime

    def validate(self, profile: InferenceProfile):  # pragma: no cover - protocol stub
        raise NotImplementedError

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.last_request = request
        return self._result


def test_safety_flags_default_fail_closed() -> None:
    flags = SupervisorSafetyFlags()
    assert flags.obs_complete is False
    assert flags.scope_ok is False
    assert flags.lease_valid is False
    assert flags.cleanup_required is True
    assert flags.active_objection_block is False
    assert flags.retry_budget_remaining == 0


def test_lift_supervisor_telemetry_integrates_progress_checkpoint_and_decision_block() -> None:
    monitor = ProgressMonitor()
    step = AttemptStep(
        step_id="s1",
        action="run_tests",
        arguments={"target": "tests/test_a.py"},
        outcome=StepOutcome.FAILURE,
        progress=ProgressKind.VERIFICATION,
        result_fingerprint="err-1",
    )
    assert monitor.observe(step) is None
    intervention = monitor.observe(step.model_copy(update={"step_id": "s2"}))
    assert intervention is not None
    assert intervention.reason == "repeated_failure_pair"

    checkpoint = WorkCheckpoint(contract_id="wc-42", unresolved_failures=["f1: assertion error"])
    digest = TaskDigest(contract_id="wc-42", text="Contract wc-42 digest", max_chars=256)

    engine = DecisionEngine()
    record = DecisionRecord(id="dec-1", proposal="retry attempt", participants=frozenset({"NAV-01", "ENG-01"}))
    record = engine.capture_initial(
        record,
        DecisionPosition(participant="ENG-01", position=Position.BLOCK, category="safety", rationale="dirty workspace"),
    )

    telemetry = lift_supervisor_telemetry(
        contract_id="wc-42",
        attempt_id="att-9",
        intervention=intervention,
        digest=digest,
        checkpoint=checkpoint,
        decision_record=record,
        retry_budget_remaining=1,
        allowed_retry_constraints=["narrow_test_scope", "narrow_test_scope"],
        allowed_escalation_targets=["plan:local-larger"],
    )
    assert telemetry.flags.active_objection_block is True
    assert telemetry.unresolved_failure_count == 1
    assert telemetry.allowed_retry_constraints == ("narrow_test_scope",)
    assert "[CONTROL-PLANE TELEMETRY]" in telemetry.render_prompt()

    with pytest.raises(ValueError, match="TaskDigest contract_id"):
        lift_supervisor_telemetry(
            contract_id="wc-other",
            attempt_id="att-9",
            intervention=intervention,
            digest=digest,
        )


def test_render_prompt_redacts_summary_when_observation_incomplete() -> None:
    telemetry = _sample_telemetry(obs_complete=False)
    prompt = telemetry.render_prompt()
    assert "obs_complete=0" in prompt
    assert "DIGEST_SUMMARY: <INCOMPLETE>" in prompt
    assert "Unit test failed" not in prompt


@pytest.mark.parametrize(
    ("raw_output", "expected_reason"),
    [
        ("", SupervisorRejectionReason.PARSE_ERROR),
        ("<think>maybe retry</think>{\"diagnosis\":\"ok\",\"action\":\"HALT_FOR_HUMAN\",\"target\":\"NONE\"}", SupervisorRejectionReason.THINK_TAG_LEAK),
        ("```json\n{\"diagnosis\":\"ok\",\"action\":\"HALT_FOR_HUMAN\",\"target\":\"NONE\"}\n```", SupervisorRejectionReason.PARSE_ERROR),
        ('{"diagnosis":"a","diagnosis":"b","action":"HALT_FOR_HUMAN","target":"NONE"}', SupervisorRejectionReason.PARSE_ERROR),
        ('{"diagnosis":"ok","action":"HALT_FOR_HUMAN","target":"NONE","extra":"x"}', SupervisorRejectionReason.SCHEMA_VIOLATION),
        ('{"diagnosis":"ok","action":"RUN_SHELL","target":"NONE"}', SupervisorRejectionReason.SCHEMA_VIOLATION),
        (json.dumps({"diagnosis": "x" * 200, "action": "HALT_FOR_HUMAN", "target": "NONE"}), SupervisorRejectionReason.DIAGNOSIS_TOO_LONG),
    ],
)
def test_parse_supervisor_output_rejects_malformed_and_think_leaks(
    raw_output: str, expected_reason: SupervisorRejectionReason
) -> None:
    proposal, reason, _ = parse_supervisor_output(raw_output)
    assert proposal is None
    assert reason is expected_reason


def test_parse_supervisor_output_accepts_strict_json_and_three_line_format() -> None:
    p_json, r_json, _ = parse_supervisor_output(
        '{"diagnosis":"Repeat failure isolated.","action":"RETRY_CONSTRAINED","target":"narrow_test_scope"}'
    )
    assert r_json is None and p_json is not None
    assert p_json.action is SupervisorAction.RETRY_CONSTRAINED
    assert p_json.target == "narrow_test_scope"

    p_lines, r_lines, _ = parse_supervisor_output(
        "DIAGNOSIS: Repeat failure isolated.\nACTION: ESCALATE_EMBODIMENT\nTARGET: plan:local-larger"
    )
    assert r_lines is None and p_lines is not None
    assert p_lines.action is SupervisorAction.ESCALATE_EMBODIMENT
    assert p_lines.target == "plan:local-larger"


@pytest.mark.parametrize(
    ("flag_override", "expected_reason"),
    [
        ({"obs_complete": False}, SupervisorRejectionReason.OBSERVATION_INCOMPLETE),
        ({"scope_ok": False}, SupervisorRejectionReason.SCOPE_VIOLATION),
        ({"cleanup_required": True}, SupervisorRejectionReason.CLEANUP_REQUIRED),
        ({"lease_valid": False}, SupervisorRejectionReason.LEASE_INVALID),
        ({"active_objection_block": True}, SupervisorRejectionReason.ACTIVE_OBJECTION_BLOCK),
        ({"retry_budget_remaining": 0}, SupervisorRejectionReason.RETRY_BUDGET_EXHAUSTED),
    ],
)
def test_gate_fails_closed_on_retry_under_every_hazard(
    flag_override: dict[str, object], expected_reason: SupervisorRejectionReason
) -> None:
    telemetry = _sample_telemetry(**flag_override)
    raw = '{"diagnosis":"Retry now.","action":"RETRY_CONSTRAINED","target":"narrow_test_scope"}'
    decision = evaluate_supervisor_proposal(telemetry, raw)
    assert decision.failed_closed is True
    assert decision.effective_action is SupervisorAction.HALT_FOR_HUMAN
    assert decision.effective_target == "NONE"
    assert decision.proposed_action is SupervisorAction.RETRY_CONSTRAINED
    assert decision.rejection_reason is expected_reason


def test_gate_enforces_target_allowlist_and_halt_target_none() -> None:
    telemetry = _sample_telemetry()

    # Prompt-injected / unadmitted retry constraint fails closed
    injected = evaluate_supervisor_proposal(
        telemetry,
        '{"diagnosis":"Bypass scope.","action":"RETRY_CONSTRAINED","target":"disable_sandbox"}',
    )
    assert injected.failed_closed is True
    assert injected.effective_action is SupervisorAction.HALT_FOR_HUMAN
    assert injected.rejection_reason is SupervisorRejectionReason.TARGET_NOT_ADMITTED

    # Clean retry succeeds
    ok_retry = evaluate_supervisor_proposal(
        telemetry,
        '{"diagnosis":"Narrow test target.","action":"RETRY_CONSTRAINED","target":"narrow_test_scope"}',
    )
    assert ok_retry.failed_closed is False
    assert ok_retry.effective_action is SupervisorAction.RETRY_CONSTRAINED
    assert ok_retry.effective_target == "narrow_test_scope"

    # Escalation allowed when lease lost (default policy), blocked when cleanup required
    esc_lease_lost = evaluate_supervisor_proposal(
        _sample_telemetry(lease_valid=False, retry_budget_remaining=0),
        '{"diagnosis":"Lease expired; escalate.","action":"ESCALATE_EMBODIMENT","target":"plan:local-larger"}',
    )
    assert esc_lease_lost.failed_closed is False
    assert esc_lease_lost.effective_action is SupervisorAction.ESCALATE_EMBODIMENT

    esc_blocked = evaluate_supervisor_proposal(
        _sample_telemetry(lease_valid=False, cleanup_required=True),
        '{"diagnosis":"Escalate anyway.","action":"ESCALATE_EMBODIMENT","target":"plan:local-larger"}',
        policy=SupervisorGatePolicy(allow_escalation_when_lease_invalid=True),
    )
    assert esc_blocked.failed_closed is True
    assert esc_blocked.rejection_reason is SupervisorRejectionReason.CLEANUP_REQUIRED

    # HALT_FOR_HUMAN with non-NONE target fails closed with UNEXPECTED_TARGET
    bad_halt = evaluate_supervisor_proposal(
        telemetry,
        '{"diagnosis":"Stop.","action":"HALT_FOR_HUMAN","target":"plan:local-larger"}',
    )
    assert bad_halt.failed_closed is True
    assert bad_halt.effective_action is SupervisorAction.HALT_FOR_HUMAN
    assert bad_halt.effective_target == "NONE"
    assert bad_halt.rejection_reason is SupervisorRejectionReason.UNEXPECTED_TARGET


def test_llama_server_schema_and_execution_request_bounds() -> None:
    profile = _sample_profile()
    telemetry = _sample_telemetry()
    req = build_supervisor_execution_request(telemetry, profile, request_id="req-sup-1", seed=7)
    assert req.max_tokens == SUPERVISOR_MAX_TOKENS
    assert req.temperature == 0.0
    assert req.context_tokens == 512
    assert req.prompt_mode == "rendered"

    payload = _completion_payload(req, [1, 2, 3])
    assert payload["n_predict"] == SUPERVISOR_MAX_TOKENS
    assert payload["json_schema"]["required"] == ["diagnosis", "action", "target"]
    assert payload["json_schema"]["additionalProperties"] is False

    with pytest.raises(ValueError, match="supervisor max_tokens"):
        build_supervisor_execution_request(telemetry, profile, request_id="req-bad", max_tokens=128)


def test_run_micro_supervisor_and_event_store_roundtrip(tmp_path: Path) -> None:
    profile = _sample_profile()
    telemetry = _sample_telemetry()
    runtime = RuntimeIdentity(runtime_id="llama.cpp", version="b4000", build_id="abc123", backends=["cpu"])

    # Successful adapter call
    ok_adapter = _FakeAdapter(
        ExecutionResult(
            request_id="req-1",
            runtime=runtime,
            success=True,
            output='{"diagnosis":"Bounded retry.","action":"RETRY_CONSTRAINED","target":"narrow_test_scope"}',
        )
    )
    decision = run_micro_supervisor(telemetry, ok_adapter, profile, request_id="req-1", seed=42)
    assert decision.failed_closed is False
    assert decision.effective_action is SupervisorAction.RETRY_CONSTRAINED
    assert decision.runtime_fingerprint == runtime.fingerprint
    assert decision.profile_id == profile.id
    assert decision.artifact_key == profile.artifact.key

    # Failed adapter call fails closed
    fail_adapter = _FakeAdapter(
        ExecutionResult(
            request_id="req-2",
            runtime=runtime,
            success=False,
            error_class=FailureClass.TIMEOUT,
            error_detail="deadline expired",
        )
    )
    failed = run_micro_supervisor(telemetry, fail_adapter, profile, request_id="req-2", seed=42)
    assert failed.failed_closed is True
    assert failed.effective_action is SupervisorAction.HALT_FOR_HUMAN
    assert failed.rejection_reason is SupervisorRejectionReason.RUNTIME_EXECUTION_FAILED

    # EventStore provenance roundtrip & hash-chain verification
    store = EventStore(tmp_path / "events.jsonl")
    store.append(supervisor_decision_event(telemetry, decision))
    store.append(supervisor_decision_event(telemetry, failed))
    replay = store.read_replay()
    assert len(replay.events) == 2
    assert replay.events[0].event_type == "work_supervisor_decision"
    assert "prompt" not in replay.events[0].payload
    assert replay.events[1].payload["failed_closed"] is True
    assert replay.events[1].payload["rejection_reason"] == "runtime_execution_failed"
    chain = store.verify_chain()
    assert chain.complete is True
    assert chain.integrity_status == "locally_consistent_unanchored"
