"""The delivered view has keyed identity and an explicit RPC acknowledgement."""
from collections import deque
from dataclasses import replace
from datetime import timedelta

import pytest
from pi_worker_fixture import admit_prompt
from pydantic import ValidationError
from test_harness_trajectory import NOW, reference
from test_pi_worker import setup_guard

from long_haul.adapters.harness_trace import TraceAdmissionDenied
from long_haul.adapters.pi_harness import PiCodingHarness, PiPrompt
from long_haul.resource_policy import ResourceLifecycle
from long_haul.work.harness import HarnessCleanup, HarnessResult


def adapter(tmp_path, prompt):
    _, attempt, normalizer, root, registry, _ = setup_guard(tmp_path)
    records = deque([
        {"type": "response", "id": "long-haul-get_state", "command": "get_state", "success": True,
         "data": {"sessionId": "synthetic-native-session", "isStreaming": False}},
        {"type": "response", "id": "long-haul-set_auto_retry", "command": "set_auto_retry", "success": True},
        {"type": "response", "id": "long-haul-set_auto_compaction", "command": "set_auto_compaction", "success": True},
    ])
    sent = []
    try:
        text = prompt(NOW)
    except TraceAdmissionDenied:
        text = "never-delivered-placeholder"
    harness = PiCodingHarness(normalizer, registry, root, sent.append,
                              lambda: records.popleft() if records else None,
                              PiPrompt(prompt, reference("template"), admit_prompt(normalizer, text)))
    harness.start(attempt)
    for n in range(1, 4):
        harness.poll(NOW, n)
    return harness, records, sent


def prompt_response(*, disposition="started", success=True):
    return {"type": "response", "id": "long-haul-prompt", "command": "prompt", "success": success,
            "data": {"disposition": disposition}}


def finish(harness):
    if harness.projector.state.terminal is None:
        harness.projector.eof(NOW, 10)
    attempt = harness.projector.state.attempt
    cleanup = HarnessCleanup(attempt_id=attempt.attempt_id, session_id=attempt.session_id,
                              boundary="sandbox", process_tree_stopped=True, evidence=reference("synthetic-cleanup"))
    return harness.finish(cleanup)


def test_distinct_actual_prompt_callbacks_produce_distinct_keyed_input_identity(tmp_path):
    first, records, sent = adapter(tmp_path / "first", lambda _: "SECRET_PROMPT_A")
    records.append(prompt_response())
    first.poll(NOW, 4)
    second, other, _ = adapter(tmp_path / "second", lambda _: "SECRET_PROMPT_B")
    other.append(prompt_response())
    second.poll(NOW, 4)
    one, two = finish(first), finish(second)
    assert one.attempt.contract_digest == two.attempt.contract_digest
    assert one.inputs[0].prompt_fingerprint != two.inputs[0].prompt_fingerprint
    assert [entry.status for entry in one.inputs] == ["requested", "acknowledged"]
    assert one.inputs[1].request.digest == one.inputs[0].fingerprint
    assert one.inputs[0].template == reference("template") and sent[-1]["message"] == "SECRET_PROMPT_A"
    history = one.model_dump_json() + "".join(item.event().model_dump_json() for item in one.inputs)
    assert "SECRET_PROMPT_A" not in history and "SECRET_PROMPT_B" not in history


@pytest.mark.parametrize("response", [None, prompt_response(success=False), prompt_response(disposition="handled")])
def test_queued_denied_or_unacknowledged_prompts_cannot_claim_delivery(tmp_path, response):
    harness, records, _ = adapter(tmp_path, lambda _: "SECRET_UNDELIVERED_PROMPT")
    if response is not None:
        records.append(response)
        harness.poll(NOW, 4)
    result = finish(harness)
    assert "acknowledged" not in [entry.status for entry in result.inputs]
    assert "SECRET_UNDELIVERED_PROMPT" not in result.model_dump_json()


def test_context_denial_before_queue_records_no_delivered_content_identity(tmp_path):
    def deny(_):
        raise TraceAdmissionDenied("synthetic current context denial")
    harness, _, sent = adapter(tmp_path, deny)
    result = finish(harness)
    assert result.terminal == "policy_denied" and [entry.status for entry in result.inputs] == ["denied"]
    assert result.inputs[0].prompt_fingerprint is None
    assert not any(command["type"] == "prompt" for command in sent)


def test_imported_input_acknowledgement_is_bound_to_exact_requested_view(tmp_path):
    harness, records, _ = adapter(tmp_path, lambda _: "SECRET")
    records.append(prompt_response())
    harness.poll(NOW, 4)
    result = finish(harness)
    payload = result.model_dump(mode="json")
    payload["inputs"][1]["prompt_fingerprint"] = "hmac-sha256:" + "b" * 64
    with pytest.raises(ValidationError, match="acknowledgement"):
        HarnessResult.model_validate(payload)


@pytest.mark.parametrize("restriction", ["revoked", "expired"])
def test_revoked_or_expired_trace_admission_precedes_first_prompt_send(tmp_path, restriction):
    _, _, normalizer, root, registry, _ = setup_guard(tmp_path)
    sent = []
    prompt = PiPrompt(lambda _: "PRIVATE_UNSENT", reference("template"), admit_prompt(normalizer, "PRIVATE_UNSENT"))
    harness = PiCodingHarness(normalizer, registry, root, sent.append, lambda: None, prompt)
    trace = normalizer.admission
    if restriction == "revoked":
        trace.profile.lifecycle = ResourceLifecycle.RETRACTED
    else:
        trace.profile.privacy.retention_until = NOW - timedelta(seconds=1)
    normalizer.admission = replace(trace, expected_policy_fingerprint=trace.profile.fingerprint)
    with pytest.raises(TraceAdmissionDenied):
        harness._begin_prompt(NOW)
    assert sent == [] and harness.inputs == []


@pytest.mark.parametrize("covered", [False, None])
def test_arbitrary_callback_text_cannot_authorize_an_uncovered_task_view(tmp_path, covered):
    _, _, normalizer, root, registry, _ = setup_guard(tmp_path)
    admission = admit_prompt(normalizer, "DIFFERENT_VIEW") if covered is False else None
    sent = []
    harness = PiCodingHarness(normalizer, registry, root, sent.append, lambda: None,
                              PiPrompt(lambda _: "PRIVATE_UNCOVERED", reference("template"), admission))
    with pytest.raises(TraceAdmissionDenied):
        harness._begin_prompt(NOW)
    assert sent == [] and [entry.status for entry in harness.inputs] == ["denied"]


def test_initial_prompt_source_restrictions_join_every_mutation(tmp_path):
    from test_harness_trajectory import start
    from test_pi_worker import request
    _, attempt, normalizer, root, registry, _ = setup_guard(tmp_path)
    prompt = PiPrompt(lambda _: "TASK_INPUT", reference("template"), admit_prompt(normalizer, "TASK_INPUT", derive=False))
    harness = PiCodingHarness(normalizer, registry, root, lambda _: None, lambda: None, prompt)
    harness._begin_prompt(NOW)  # READ/AI_AUGMENT are allowed; DERIVE is withheld.
    call = start("write", "write", {"path": "fixture/code.py", "content": "derived from task input"})
    harness.guard.start(call)
    with pytest.raises(TraceAdmissionDenied):
        harness.guard.authorize(request(attempt, call), root, NOW)
    assert (root / "fixture/code.py").read_bytes() == b"before\n"
