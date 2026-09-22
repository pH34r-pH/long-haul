from long_haul.events import Event
from long_haul.work import (
    WorkSession,
    materialize_checkpoint,
    render_digest,
    resume_session,
)


def event(kind, **payload):
    return Event(event_type=kind, payload={"contract_id": "wc-1", **payload})


def test_checkpoint_materializes_authoritative_events():
    events = [
        event("work_fact", text="pytest uses the repository virtualenv"),
        event("work_constraint", text="Do not modify deployment credentials"),
        event("work_subgoal", text="locate failure", state="remaining"),
        event("work_failure", failure_id="f1", detail="telemetry test fails"),
        event("work_subgoal", text="locate failure", state="completed"),
        event("work_subgoal", text="repair initialization", state="remaining"),
        event("work_artifact_verified", fingerprint="abc123", description="tests pass through telemetry subset"),
        event("work_failure_resolved", failure_id="f1"),
    ]
    checkpoint = materialize_checkpoint("wc-1", events)
    assert checkpoint.completed_subgoals == ["locate failure"]
    assert checkpoint.remaining_subgoals == ["repair initialization"]
    assert checkpoint.unresolved_failures == []
    assert checkpoint.artifact.fingerprint == "abc123"
    assert checkpoint.through_event_id == events[-1].id


def test_other_contract_events_are_ignored():
    foreign = Event(event_type="work_fact", payload={"contract_id": "wc-2", "text": "secret"})
    checkpoint = materialize_checkpoint("wc-1", [foreign])
    assert checkpoint.facts == []


def test_digest_is_hard_bounded_and_reports_omissions():
    events = [event("work_fact", text=f"discovery {index} " + "x" * 60) for index in range(12)]
    checkpoint = materialize_checkpoint("wc-1", events)
    digest = render_digest(checkpoint, max_chars=180)
    assert len(digest.text) <= 180
    assert digest.truncated
    assert digest.omitted_items > 0
    assert "discovery 11" in digest.text
    assert "discovery 0" not in digest.text


def test_resume_changes_session_identity_but_preserves_work_identity():
    events = [event("work_fact", text="bounded state")]
    checkpoint = materialize_checkpoint("wc-1", events)
    first = WorkSession(contract_id="wc-1", crew_id="ENG-01", execution_plan_id="local:kestrel")
    second = resume_session(first, checkpoint)
    assert second.session_id != first.session_id
    assert second.resumed_from_session_id == first.session_id
    assert second.contract_id == first.contract_id
    assert second.crew_id == first.crew_id
    assert second.execution_plan_id == first.execution_plan_id
    assert second.checkpoint_through_event_id == events[-1].id


def test_verbose_old_observations_do_not_enter_digest():
    noisy = Event(event_type="observation", payload={"contract_id": "wc-1", "text": "x" * 10000})
    useful = event("work_fact", text="the actionable compact fact")
    digest = render_digest(materialize_checkpoint("wc-1", [noisy, useful]), max_chars=200)
    assert "actionable compact fact" in digest.text
    assert "x" * 100 not in digest.text
