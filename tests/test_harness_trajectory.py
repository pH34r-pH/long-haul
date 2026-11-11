"""Hermetic wire/metadata fixtures; no Pi install, subprocess, network, or model."""
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from long_haul.adapters.fixture_harness import FixtureCodingHarness
from long_haul.adapters.harness_trace import (
    TraceAdmissionDenied,
    TraceArgumentsInvalid,
    TraceNormalizationPolicy,
    TraceNormalizer,
    TraceUseAdmission,
    supported_pi_tools,
)
from long_haul.adapters.pi_trajectory import PiTrajectoryProjector
from long_haul.events.store import EventStore
from long_haul.resource_policy import (
    AuthorityClass,
    PolicyRule,
    ResourceAction,
    ResourceIdentity,
    ResourceLifecycle,
    ResourcePolicyProfile,
    ResourceUseRequest,
)
from long_haul.work import (
    ProgressMonitor,
    WorkCheckpoint,
    WorkContract,
    WorkSession,
    render_digest,
)
from long_haul.work.harness import (
    CodingHarnessAdapter,
    HarnessCleanup,
    HarnessIdentity,
    HarnessResult,
    HarnessState,
    SemanticActionEventV1,
    harness_attempt,
    progress_event,
    resume_harness_attempt,
)
from long_haul.work.repository_models import DigestReference, content_digest

NOW = datetime(2026, 11, 11, tzinfo=UTC)
KEY = b"hermetic-fixture-external-key-material-256-bits"


def reference(name):
    return DigestReference(reference="urn:fixture:" + name, digest=content_digest(name.encode()))


def core(*, disclose_paths=False, disclose_native_ids=False, harness="fixture"):
    extension = {"source": {"repository_id": "fixture/repository", "visibility": "public",
                            "data_class": "public", "commit": "a" * 40, "tree": "b" * 40},
                 "task": reference("task"), "environment": reference("environment"),
                 "toolchain": reference("toolchain"), "context_manifest": reference("context"),
                 "public_evaluation": reference("evaluator")}
    contract = WorkContract(
        contract_id="contract", mission_id="mission", objective="repair fixture", allowed_paths=["fixture"],
        forbidden_paths=["fixture/private"], capabilities=["read", "edit", "search"],
        budget={"wall_seconds": 30, "max_attempts": 2}, repository_work=extension,
        success_predicates=[{"predicate_id": "tests", "kind": "test", "description": "external test",
                             "command": ["trusted-check"]}],
    )
    session = WorkSession(contract_id="contract", crew_id="crew", session_id="session")
    identity = HarnessIdentity(harness_id=harness, package_name="fixture" if harness == "fixture" else "pi",
                               package_version="1", protocol="pi-rpc-fixture-v1",
                               source=reference("source"), artifact=reference("artifact"),
                               configuration=reference("configuration"))
    attempt = harness_attempt(contract, session, identity, "attempt")
    profile = ResourcePolicyProfile(
        identity=ResourceIdentity(resource_id="urn:fixture:trace", resource_type="operational-trace"),
        authority_class=AuthorityClass.AUTHORITATIVE_CONTROL, lifecycle=ResourceLifecycle.ACTIVE,
        policy_id="urn:fixture:trace-policy", policy_assigner="urn:fixture:operator",
        permissions=[PolicyRule(rule_id="trace", actions=[ResourceAction.ANNOTATE],
                                purposes=["operational-trace"], crew_ids=["crew"], contract_ids=["contract"])],
    )
    request = ResourceUseRequest(request_id="trace-request", resource_id=profile.identity.resource_id,
                                 caller_id="session", crew_id="crew", contract_id="contract", mission_id="mission",
                                 repository_id="fixture/repository", action=ResourceAction.ANNOTATE,
                                 purpose="operational-trace", now=NOW)
    admission = TraceUseAdmission(profile, profile.fingerprint, request)
    policy = TraceNormalizationPolicy(key_reference=reference("key-reference"), disclose_paths=disclose_paths,
                                      disclose_native_ids=disclose_native_ids)
    normalizer = TraceNormalizer(contract, attempt, policy, KEY, admission)
    return contract, session, attempt, normalizer


def start(call="call-1", tool="read", args=None):
    return {"type": "tool_execution_start", "toolCallId": call, "toolName": tool,
            "args": {"path": "fixture/code.py"} if args is None else args}


def end(call="call-1", tool="read", *, error=False, result=None):
    return {"type": "tool_execution_end", "toolCallId": call, "toolName": tool,
            "isError": error, "result": {"content": [{"type": "text", "text": "private canary"}]}
            if result is None else result}


def feed(projector, records):
    for n, record in enumerate(records, 1):
        projector.feed(record, NOW + timedelta(milliseconds=n), n * 1000000)
    return projector.events


def cleanup(attempt, *, stopped=True, boundary="fixture"):
    return HarnessCleanup(attempt_id=attempt.attempt_id, session_id=attempt.session_id,
                          boundary=boundary, process_tree_stopped=stopped, evidence=reference("cleanup"))


def test_correlated_ordered_actions_preserve_ids_without_private_payloads(tmp_path):
    _, _, _, normalizer = core()
    projector = PiTrajectoryProjector(normalizer)
    events = feed(projector, [start(), end(), {"type": "agent_end", "messages": ["SECRET_REASONING"]},
                              {"type": "agent_settled", "aborted": False}])
    assert [e.phase for e in events] == ["start", "end", "lifecycle"]
    assert [e.sequence for e in events] == [1, 2, 3]
    assert events[1].native_id == normalizer.native_id("call-1")[0] and events[1].duration_seconds == .001
    assert events[1].arguments.target_fingerprint and events[1].arguments.argument_fingerprint
    assert events[1].arguments.path is None
    assert projector.state.terminal == "settled"
    store = EventStore(tmp_path / "events.jsonl")
    for event in events:
        store.append(event.event())
    history = store.export_jsonl()
    for canary in ["private canary", "SECRET_REASONING", "fixture/code.py", KEY.decode()]:
        assert canary not in history
    assert [e.payload["semantic_action"]["sequence"] for e in store.events()] == [1, 2, 3]


def test_equivalent_target_and_default_arguments_have_same_keyed_signature():
    _, _, _, normalizer = core(disclose_paths=True)
    first = normalizer.normalize("read", {"path": "./fixture/code.py"}, NOW)[2]
    second = normalizer.normalize("read", {"path": "/workspace/fixture/code.py", "offset": 1}, NOW)[2]
    assert first == second and first.path == "fixture/code.py"
    _, _, attempt, other = core()
    changed = TraceNormalizer(other.contract, attempt, other.policy, b"another-fixture-key-material-32bytes", other.admission)
    assert changed.identity != other.identity
    assert changed.normalize("read", {"path": "fixture/code.py"}, NOW)[2].argument_fingerprint != first.argument_fingerprint


@pytest.mark.parametrize("path", ["../private", "/etc/passwd", "fixture/../other", ".git/config", "fixture/private/key"])
def test_scope_traversal_and_forbidden_targets_fail_without_retaining_them(path):
    _, _, _, normalizer = core()
    projector = PiTrajectoryProjector(normalizer)
    projector.feed(start(args={"path": path}), NOW, 1)
    assert projector.state.terminal == "protocol_error"
    assert len(projector.events) == 1 and projector.events[0].summary == "malformed_record"
    assert path not in projector.events[0].model_dump_json()


def test_content_query_and_result_fingerprints_are_not_raw_payloads():
    _, _, _, normalizer = core()
    secret = "Bearer SUPER_SECRET password=very-private https://private.invalid/token"
    write = normalizer.normalize("write", {"path": "fixture/code.py", "content": secret}, NOW)[2]
    grep = normalizer.normalize("grep", {"path": "fixture", "pattern": secret}, NOW)[2]
    assert write.content_fingerprint and grep.query_fingerprint
    assert secret not in write.model_dump_json() + grep.model_dump_json() + normalizer.result({"text": secret})
    with pytest.raises(TraceArgumentsInvalid):
        normalizer.normalize("read", {"path": "fixture/code.py", "environment": {"TOKEN": secret}}, NOW)
    with pytest.raises(TraceArgumentsInvalid):
        normalizer.normalize("read", {"path": "fixture/code.py", "offset": None}, NOW)


def test_repeated_equivalent_reads_and_failures_feed_existing_progress_monitor():
    _, _, _, normalizer = core()
    projector, monitor = PiTrajectoryProjector(normalizer), ProgressMonitor()
    interventions = []
    for n in range(3):
        projector.feed(start(str(n), args={"path": "./fixture/code.py", "offset": 1}), NOW, n * 2)
        ended = projector.feed(end(str(n), error=True), NOW, n * 2 + 1)[0]
        interventions.append(progress_event(monitor, ended))
    assert interventions[1].payload["reason"] == "repeated_failure_pair"
    assert "private canary" not in interventions[1].model_dump_json()


def test_only_measured_candidate_transition_can_count_as_mutation_progress():
    _, _, _, normalizer = core()
    projector = PiTrajectoryProjector(normalizer)
    projector.feed(start(tool="write", args={"path": "fixture/code.py", "content": "new"}), NOW, 1,
                   candidate_before=reference("before"))
    observed = projector.feed(end(tool="write", result={"details": {"fingerprint": "SELF_APPROVED"}}), NOW, 2)[0]
    assert observed.progress.value == "no_progress" and observed.candidate_after is None
    projector.feed(start("second", "write", {"path": "fixture/code.py", "content": "newer"}), NOW, 3,
                   candidate_before=reference("before"))
    measured = projector.feed(end("second", "write"), NOW, 4, candidate_after=reference("after"))[0]
    assert measured.progress.value == "state_change"
    assert "SELF_APPROVED" not in observed.model_dump_json()


@pytest.mark.parametrize("records", [
    [end()], [start(), start()], [start(), end(tool="write")],
    [start(), {**end(), "isError": "false"}],
    [start(call="https://private.invalid")],
])
def test_malformed_or_uncorrelated_events_terminate_explicitly(records):
    _, _, _, normalizer = core()
    projector = PiTrajectoryProjector(normalizer)
    feed(projector, records)
    assert projector.state.terminal == "protocol_error"


def test_unknown_tool_is_visible_and_never_classified_as_a_harmless_read():
    _, _, _, normalizer = core()
    projector = PiTrajectoryProjector(normalizer)
    feed(projector, [start(tool="bash", args={"command": "publish private canary"})])
    assert projector.state.terminal == "protocol_error"
    assert projector.events[0].action == "unknown" and projector.events[0].consequence == "unknown"
    assert "publish private canary" not in projector.events[0].model_dump_json()


@pytest.mark.parametrize("capabilities", [["test"], ["shell"], ["read", "test"], ["deploy"]])
def test_supported_profile_declines_shell_and_test_only_bash_projection(capabilities):
    contract, _, _, _ = core()
    contract.capabilities = capabilities
    with pytest.raises(ValueError, match="does not admit"):
        supported_pi_tools(contract)


def test_agent_end_is_not_terminal_and_recovery_does_not_copy_messages():
    _, _, _, normalizer = core()
    projector = PiTrajectoryProjector(normalizer)
    feed(projector, [{"type": "agent_end"}])
    assert projector.state.terminal is None
    projector.feed({"type": "auto_retry_start", "errorMessage": "private canary"}, NOW + timedelta(seconds=1), 2000000)
    projector.feed({"type": "auto_retry_end", "success": True}, NOW + timedelta(seconds=2), 3000000)
    projector.feed({"type": "agent_settled", "aborted": False}, NOW + timedelta(seconds=3), 4000000)
    assert projector.state.terminal == "settled"
    assert [e.summary for e in projector.events] == ["retry_started", "retry_completed", "settled"]
    assert "private canary" not in json.dumps([e.model_dump(mode="json") for e in projector.events])


@pytest.mark.parametrize("failure,terminal", [
    ({"type": "auto_retry_end", "success": False, "finalError": "secret"}, "provider_failure"),
    ({"type": "compaction_end", "reason": "overflow", "errorMessage": "secret"}, "context_exhausted"),
])
def test_provider_and_context_failures_remain_distinct(failure, terminal):
    _, _, _, normalizer = core()
    projector = PiTrajectoryProjector(normalizer)
    feed(projector, [failure, {"type": "agent_settled", "aborted": False}])
    assert projector.state.terminal == terminal


def test_current_policy_expiry_revocation_and_scope_are_enforced_before_projection():
    _, _, attempt, normalizer = core()
    grant = normalizer.admission
    with pytest.raises(TraceAdmissionDenied):
        replace(grant, request=grant.request.model_copy(update={"crew_id": "other"})).authorize(attempt, NOW)
    grant.profile.privacy.retention_until = NOW + timedelta(seconds=1)
    pinned = replace(grant, expected_policy_fingerprint=grant.profile.fingerprint)
    assert pinned.authorize(attempt, NOW)
    with pytest.raises(TraceAdmissionDenied):
        pinned.authorize(attempt, NOW + timedelta(seconds=2))
    grant.profile.lifecycle = ResourceLifecycle.RETRACTED
    projector = PiTrajectoryProjector(normalizer)
    projector.feed(start(), NOW, 1)
    assert projector.state.terminal == "policy_denied" and not projector.events


def test_fixture_adapter_and_cleanup_before_freeze_are_not_real_qualification():
    _, _, attempt, normalizer = core()
    adapter = FixtureCodingHarness(normalizer, (start(), end(), {"type": "agent_settled", "aborted": False}))
    assert isinstance(adapter, CodingHarnessAdapter)
    adapter.start(attempt)
    with pytest.raises(ValueError, match="unfinished"):
        adapter.finish(cleanup(attempt), reference("candidate"))
    for n in range(3):
        adapter.poll(NOW, n)
    with pytest.raises(ValueError, match="cleanup"):
        adapter.finish(cleanup(attempt, stopped=False), reference("candidate"))
    result = adapter.finish(cleanup(attempt), reference("candidate"))
    assert result.acceptance == "not_evaluated" and result.candidate_disposition == "frozen"
    with pytest.raises(ValueError):
        adapter.finish(cleanup(attempt))
    _, _, _, real = core(harness="pi")
    with pytest.raises(ValueError, match="real harness"):
        FixtureCodingHarness(real, ())


def test_cancellation_pending_actions_and_crashes_retain_discarded_attempts():
    _, _, attempt, normalizer = core()
    adapter = FixtureCodingHarness(normalizer, (start(),))
    adapter.start(attempt)
    adapter.poll(NOW, 1)
    adapter.cancel()
    adapter.poll(NOW, 2)
    result = adapter.finish(cleanup(attempt), reference("discarded-candidate"))
    assert result.terminal == "cancelled" and result.candidate_disposition == "discarded"
    assert adapter.projector.events[-2].summary == "interrupted"
    assert adapter.projector.events[-1].summary == "settled"
    crashed = PiTrajectoryProjector(normalizer)
    crashed.eof(NOW, 1)
    assert crashed.state.terminal == "harness_crash"


def test_resume_is_fresh_and_retains_checkpoint_and_attempt_provenance():
    contract, session, attempt, _normalizer = core()
    state = HarnessState(attempt)
    state.settle("provider_failure")
    previous = state.finish((), cleanup(attempt), reference("discarded"))
    checkpoint = WorkCheckpoint(contract_id=contract.contract_id, through_event_id="event-1")
    digest = render_digest(checkpoint)
    digest.context_manifest = contract.repository_work.context_manifest
    resumed, new_session = resume_harness_attempt(previous, session, checkpoint, digest, contract,
                                                  attempt.harness, "attempt-2")
    assert new_session.session_id != session.session_id
    assert new_session.resumed_from_session_id == session.session_id
    assert resumed.previous_attempt.digest == previous.fingerprint
    assert resumed.checkpoint and resumed.digest and resumed.contract_digest == attempt.contract_digest


def test_bounded_streams_and_strict_models_fail_visibly():
    _, _, _, normalizer = core()
    projector = PiTrajectoryProjector(normalizer, max_records=1)
    feed(projector, [{"type": "message_update", "thinking": "never persist"}, {"type": "message_update"}])
    assert projector.state.terminal == "resource_exhausted" and not projector.events
    with pytest.raises(ValueError, match="256 bits"):
        TraceNormalizer(normalizer.contract, normalizer.attempt, normalizer.policy, b"weak", normalizer.admission)
    event = feed(PiTrajectoryProjector(normalizer), [start()])[0]
    with pytest.raises(ValidationError):
        SemanticActionEventV1.model_validate({**event.model_dump(), "raw_arguments": {"secret": "no"}})


def test_fixture_replay_is_deterministic_and_search_defaults_are_canonical():
    _, _, _, normalizer = core()
    first = feed(PiTrajectoryProjector(normalizer), [start(), end(), {"type": "agent_settled", "aborted": False}])
    second = feed(PiTrajectoryProjector(normalizer), [start(), end(), {"type": "agent_settled", "aborted": False}])
    assert first == second
    implicit = normalizer.normalize("grep", {"path": "./fixture", "pattern": "secret query"}, NOW)[2]
    explicit = normalizer.normalize("grep", {"pattern": "secret query", "path": "fixture", "context": 0,
                                               "literal": False, "ignoreCase": False, "limit": 100}, NOW)[2]
    assert implicit == explicit


def test_resume_budget_and_trace_provenance_cannot_be_bypassed():
    contract, session, attempt, normalizer = core()
    last = harness_attempt(contract, session, attempt.harness, "last-attempt", attempt_number=2)
    state = HarnessState(last)
    state.settle("settled")
    result = state.finish((), cleanup(last))
    checkpoint = WorkCheckpoint(contract_id=contract.contract_id)
    digest = render_digest(checkpoint)
    digest.context_manifest = contract.repository_work.context_manifest
    with pytest.raises(ValueError, match="retry budget"):
        resume_harness_attempt(result, session, checkpoint, digest, contract, attempt.harness, "third-attempt")
    event = feed(PiTrajectoryProjector(normalizer), [start()])[0]
    invalid = HarnessState(attempt)
    invalid.settle("settled")
    with pytest.raises(ValueError, match="provenance"):
        invalid.finish((event.model_copy(update={"attempt_id": "forged"}),), cleanup(attempt))
    with pytest.raises(ValidationError):
        HarnessCleanup(attempt_id="attempt", session_id="session", boundary="fixture",
                       process_tree_stopped="true", evidence=reference("cleanup"))


def test_deadlines_are_executor_observations_and_cancel_does_not_hide_protocol_failure():
    _, _, _, normalizer = core()
    timed = PiTrajectoryProjector(normalizer)
    timed.interrupt("timed_out", NOW, 1)
    assert timed.state.terminal == "timed_out"
    malformed = PiTrajectoryProjector(normalizer)
    malformed.state.cancel()
    malformed.feed(end(), NOW, 1)
    assert malformed.state.terminal == "protocol_error"


@pytest.mark.parametrize("mutation", [
    {"cleanup": {"attempt_id": "other-attempt"}},
    {"cleanup": {"session_id": "other-session"}},
    {"cleanup": {"boundary": "sandbox"}},
    {"cleanup": {"process_tree_stopped": False}},
    {"terminal": "cancelled", "candidate_disposition": "frozen"},
    {"terminal": "provider_failure", "candidate_disposition": "frozen"},
    {"candidate": None, "candidate_disposition": "frozen"},
    {"candidate_disposition": "none"},
])
def test_imported_attempt_result_cannot_mismatch_cleanup_or_candidate_state(mutation):
    _, _, attempt, _ = core()
    state = HarnessState(attempt)
    state.settle("settled")
    valid = state.finish((), cleanup(attempt), reference("candidate"))
    payload = valid.model_dump(mode="json")
    for key, value in mutation.items():
        if key == "cleanup":
            payload[key].update(value)
        else:
            payload[key] = value
    with pytest.raises(ValidationError):
        HarnessResult.model_validate(payload)


def test_model_copy_cannot_bypass_cleanup_validation_at_resume():
    contract, session, attempt, _ = core()
    state = HarnessState(attempt)
    state.settle("settled")
    previous = state.finish((), cleanup(attempt))
    forged = previous.model_copy(update={"cleanup": previous.cleanup.model_copy(update={"attempt_id": "wrong"})})
    checkpoint = WorkCheckpoint(contract_id=contract.contract_id)
    digest = render_digest(checkpoint)
    digest.context_manifest = contract.repository_work.context_manifest
    with pytest.raises(ValidationError):
        resume_harness_attempt(forged, session, checkpoint, digest, contract, attempt.harness, "attempt-2")


@pytest.mark.parametrize("change", ["expiry", "revocation"])
def test_annotation_denial_mid_call_preserves_history_without_new_argument_events(change):
    _, _, attempt, normalizer = core(disclose_paths=True)
    grant = normalizer.admission
    if change == "expiry":
        grant.profile.privacy.retention_until = NOW + timedelta(seconds=1)
        normalizer.admission = replace(grant, expected_policy_fingerprint=grant.profile.fingerprint)
    projector = PiTrajectoryProjector(normalizer)
    projector.feed(start(), NOW, 1)
    history = projector.events
    assert history[0].arguments.path == "fixture/code.py"
    if change == "revocation":
        grant.profile.lifecycle = ResourceLifecycle.RETRACTED
        normalizer.admission = replace(grant, expected_policy_fingerprint=grant.profile.fingerprint)
    added = projector.feed(end(), NOW + timedelta(seconds=2), 2)
    assert added == () and projector.events == history
    assert projector.state.terminal == "policy_denied"
    with pytest.raises(ValueError, match="cleanup"):
        projector.state.finish(projector.events, cleanup(attempt, stopped=False))


def test_native_call_id_secret_carrier_is_keyed_by_default():
    _, _, _, normalizer = core()
    private_id = "SYNTHETIC_PRIVATE_NATIVE_ID_0123456789"
    projector = PiTrajectoryProjector(normalizer)
    events = feed(projector, [start(private_id), end(private_id)])
    assert events[0].native_id == events[1].native_id
    assert events[0].native_id != private_id
    assert private_id not in json.dumps([event.model_dump(mode="json") for event in events])
    assert events[0].native_id_mode == "keyed"
    _, _, _, disclosed = core(disclose_native_ids=True)
    assert disclosed.native_id(private_id) == (private_id, "raw")
