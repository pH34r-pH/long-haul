"""Required hosted gate: actual pinned Pi + deterministic provider, not a real LLM."""
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pi_worker_fixture import PLAN, admit_prompt, worker_setup
from repository_evaluator_fixture import PROGRAM, environment

from long_haul.adapters.harness_trace import TraceNormalizer
from long_haul.adapters.pi_rpc import pi_prompt
from long_haul.events.store import EventStore
from long_haul.work import WorkCheckpoint, WorkSession, render_digest
from long_haul.work.harness import resume_harness_attempt
from long_haul.work.repository_evaluator import RepositoryEvaluationRunner
from long_haul.work.repository_sandbox import DockerGVisorSandbox

pytestmark = pytest.mark.skipif(os.environ.get("LONG_HAUL_REQUIRE_PI_GVISOR") != "1",
                                reason="actual Pi/gVisor qualification is mandatory in its hosted CI job")


def evidence(result, events):
    print(json.dumps({"qualification": "actual-pi-synthetic-provider-gvisor", "real_model": False,
                      "result": result.model_dump(mode="json"),
                      "events": [event.model_dump(mode="json") for event in events]}))


def test_actual_pi_repair_freezes_after_cleanup_then_independent_evaluator(tmp_path):
    worker, request, profile = worker_setup(tmp_path, PLAN)
    result, events, snapshot = worker.run(request)
    evidence(result, events)
    assert result.terminal == "settled" and result.candidate_disposition == "frozen" and snapshot is not None
    assert result.acceptance == "not_evaluated" and result.cleanup.process_tree_stopped
    assert [entry.status for entry in result.inputs] == ["requested", "acknowledged"]
    assert [event.tool for event in events if event.phase == "end"] == ["read", "edit"]
    assert all(event.execution_admission is not None for event in events if event.phase == "end")
    assert b"a + b" in snapshot.contents["program.py"]
    sandbox = DockerGVisorSandbox(environment(), Path(os.environ["LONG_HAUL_RUNSC"]))
    packet, evaluation = RepositoryEvaluationRunner(sandbox).evaluate(
        request.normalizer.contract, snapshot, profile, attempt_id="independent-after-pi", packet_id="independent-after-pi")
    assert evaluation.disposition.value == "ACCEPTED" and packet.validity == "valid"
    print(json.dumps({"qualification": "independent-evaluation-after-actual-pi", "packet": packet.model_dump(mode="json")}))


def test_actual_pi_write_read_edit_keeps_derived_policy_and_operation_identity(tmp_path):
    plan = {"mode": "success", "actions": [
        {"tool": "write", "args": {"path": "program.py", "content": PROGRAM.decode()}},
        {"tool": "read", "args": {"path": "program.py"}},
        {"tool": "edit", "args": {"path": "program.py", "edits": [{"oldText": "a + b", "newText": "sum((a, b))"}]}},
    ]}
    worker, request, _ = worker_setup(tmp_path, plan)
    result, events, snapshot = worker.run(request)
    evidence(result, events)
    assert result.terminal == "settled" and snapshot is not None
    assert [event.tool for event in events if event.phase == "end"] == ["write", "read", "edit"]
    assert all(event.execution_admission is not None for event in events if event.phase == "end")
    assert b"sum((a, b))" in snapshot.contents["program.py"]
    assert worker.last_derived_bindings["program.py"].identity.content_digest == snapshot.candidate.files[1].digest


@pytest.mark.parametrize("plan,expected", [
    ({"mode": "success", "actions": [{"tool": "write", "args": {"path": "/control/launch.json", "content": "tamper"}}]}, "protocol_error"),
    ({"mode": "success", "actions": [{"tool": "write", "args": {"path": "/proc/1/fd/1", "content": "forged"}}]}, "protocol_error"),
    ({"mode": "provider_failure", "actions": []}, "provider_failure"),
    ({"mode": "hang", "actions": []}, "timed_out"),
])
def test_real_pi_denial_failure_and_deadline_discard_candidates(tmp_path, plan, expected):
    worker, request, _ = worker_setup(tmp_path, plan, seconds=8)
    result, events, snapshot = worker.run(request)
    evidence(result, events)
    assert result.terminal == expected and snapshot is None and result.candidate is None
    assert result.cleanup.process_tree_stopped and result.acceptance == "not_evaluated"


def test_actual_pi_cancel_cleans_process_tree_before_any_candidate(tmp_path):
    worker, request, _ = worker_setup(tmp_path, {"mode": "hang", "actions": []}, seconds=10)
    result, events, snapshot = worker.run(replace(request, cancel_after=3))
    evidence(result, events)
    assert result.terminal == "cancelled" and snapshot is None and result.cleanup.process_tree_stopped


def test_native_policy_denial_precedes_filesystem_change(tmp_path):
    plan = {"mode": "success", "actions": [{"tool": "write", "args": {"path": "program.py", "content": "tamper"}}]}
    worker, request, _ = worker_setup(tmp_path, plan)
    request.registry["program.py"].profile.lifecycle = "retracted"
    result, events, snapshot = worker.run(request)
    evidence(result, events)
    assert result.terminal == "policy_denied" and snapshot is None
    assert all(event.phase != "end" or event.summary == "interrupted" for event in events)


def test_actual_pi_resume_starts_fresh_native_session_and_retains_candidate_history(tmp_path):
    worker, request, _ = worker_setup(tmp_path, PLAN)
    store = EventStore(tmp_path / "history.jsonl")
    previous, events, snapshot = worker.run(replace(request, event_store=store))
    first_native = worker.last_native_session_tag
    assert previous.terminal == "settled" and snapshot is not None
    old = previous.attempt
    session = WorkSession(session_id=old.session_id, crew_id=old.crew_id, contract_id=old.contract_id)
    checkpoint = WorkCheckpoint(contract_id=old.contract_id, through_event_id=events[-1].event().id)
    digest = render_digest(checkpoint)
    contract = request.normalizer.contract
    digest.context_manifest = contract.repository_work.context_manifest
    plan = {"mode": "success", "actions": [{"tool": "read", "args": {"path": "program.py"}}]}
    attempt, fresh = resume_harness_attempt(previous, session, checkpoint, digest, contract,
                                            worker.harness_identity(plan), "resumed-real-pi")
    trace = request.normalizer.admission
    trace = replace(trace, request=trace.request.model_copy(update={"caller_id": fresh.session_id,
                                                                  "now": datetime.now(UTC)}))
    normalizer = TraceNormalizer(contract, attempt, request.normalizer.policy,
                                 b"synthetic-public-qualification-only-key-256-bits", trace)
    resource = request.registry["program.py"]
    registry = {"program.py": replace(resource, derived=worker.last_derived_bindings["program.py"])}
    prompt = lambda now: pi_prompt(contract, digest, session=fresh, evaluation_time=now)
    resumed, new_events, candidate = worker.run(replace(request, base=snapshot, normalizer=normalizer,
        registry=registry, prompt=prompt, synthetic=plan, event_store=store,
        prompt_admission=admit_prompt(normalizer, prompt(datetime.now(UTC)))))
    evidence(resumed, new_events)
    assert resumed.terminal == "settled" and candidate.candidate == snapshot.candidate
    assert resumed.attempt.session_id != old.session_id and worker.last_native_session_tag != first_native
    assert resumed.attempt.previous_attempt.digest == previous.fingerprint and resumed.attempt.checkpoint
    assert len(store.events()) == len(events) + len(new_events) + len(previous.inputs) + len(resumed.inputs) + 1
    assert "a + b" not in store.export_jsonl()
