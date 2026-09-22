from long_haul.work import (
    AttemptStep,
    ProgressKind,
    ProgressMonitor,
    ProgressPolicy,
    StepOutcome,
    stable_signature,
)


def step(step_id, action="read", arguments=None, outcome=StepOutcome.SUCCESS, progress=ProgressKind.INFORMATION, result=None):
    return AttemptStep(
        step_id=step_id,
        action=action,
        arguments=arguments or {"path": "README.md"},
        outcome=outcome,
        progress=progress,
        result_fingerprint=result,
    )


def test_signatures_are_deterministic_across_argument_order():
    assert stable_signature("tool", {"b": 2, "a": 1}) == stable_signature("tool", {"a": 1, "b": 2})


def test_repeated_reads_trigger_recovery():
    monitor = ProgressMonitor(ProgressPolicy(consecutive_limit=3, repeated_call_limit=3))
    assert monitor.observe(step("1")) is None
    assert monitor.observe(step("2")) is None
    intervention = monitor.observe(step("3"))
    assert intervention.reason == "consecutive_repeated_action"


def test_repeated_successful_non_verification_calls_trigger():
    monitor = ProgressMonitor(ProgressPolicy(repeated_call_limit=3, consecutive_limit=9))
    monitor.observe(step("1", action="search", arguments={"q": "x"}))
    monitor.observe(step("2", action="other", arguments={"q": "y"}))
    monitor.observe(step("3", action="search", arguments={"q": "x"}))
    monitor.observe(step("4", action="other", arguments={"q": "z"}))
    intervention = monitor.observe(step("5", action="search", arguments={"q": "x"}))
    assert intervention.reason == "repeated_action"


def test_repeated_failure_pair_triggers_before_generic_loop():
    monitor = ProgressMonitor(ProgressPolicy(repeated_failure_limit=2, consecutive_limit=9, repeated_call_limit=9))
    failed = {"outcome": StepOutcome.FAILURE, "progress": ProgressKind.NO_PROGRESS, "result": "validation-error"}
    assert monitor.observe(step("1", action="edit", **failed)) is None
    intervention = monitor.observe(step("2", action="edit", **failed))
    assert intervention.reason == "repeated_failure_pair"


def test_legitimate_repeated_verification_does_not_trigger_or_accumulate_no_progress():
    monitor = ProgressMonitor(ProgressPolicy(consecutive_limit=9, repeated_call_limit=2, max_no_progress_steps=2))
    verify = {"action": "pytest", "progress": ProgressKind.VERIFICATION, "outcome": StepOutcome.SUCCESS}
    assert monitor.observe(step("1", **verify)) is None
    assert monitor.observe(step("2", **verify)) is None
    assert monitor.state.no_progress_steps == 0


def test_real_progress_resets_no_progress_budget():
    monitor = ProgressMonitor(ProgressPolicy(max_no_progress_steps=3, consecutive_limit=9, repeated_call_limit=9))
    monitor.observe(step("1"))
    monitor.observe(step("2", action="search"))
    assert monitor.state.no_progress_steps == 2
    monitor.observe(step("3", action="edit", progress=ProgressKind.STATE_CHANGE))
    assert monitor.state.no_progress_steps == 0


def test_no_progress_budget_can_terminate_attempt_and_emit_safe_telemetry():
    monitor = ProgressMonitor(ProgressPolicy(max_no_progress_steps=2, consecutive_limit=9, repeated_call_limit=9))
    monitor.observe(step("1", arguments={"secret_prompt": "do not emit"}))
    intervention = monitor.observe(step("2", action="search", arguments={"query": "also private"}))
    assert intervention.reason == "no_progress_budget_exhausted"
    assert intervention.terminate_attempt
    payload = monitor.recovery_event_payload("wc-1", "attempt-1", intervention)
    assert payload["terminate_attempt"] is True
    assert "secret_prompt" not in str(payload)
    assert "also private" not in str(payload)
