"""Local integration probes; not yet an authoritative security boundary."""
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from long_haul.decisions import (
    DecisionLifecycle,
    DecisionRecord,
    ObjectionLifecycle,
    ObjectionTransition,
    RaiseObjectionCommand,
    replay_decisions,
)
from long_haul.events import Event, EventStore
from long_haul.models import DecisionPosition, Position


def test_legacy_snapshot_cannot_claim_objection_dispositions():
    record = DecisionRecord(
        id="forged", proposal="test", participants={"ENG"},
        initial_positions=(DecisionPosition(participant="ENG", position=Position.BLOCK, category="safety",
                                           rationale="hazard", objection_id="unproven", authority_scope="test",
                                           applicability_scope="test", applicability_revision=1),),
        resolved_objection_ids=("unproven",), resolution="consent", action="proceed",
    )
    event = Event(event_type="decision", actor="ENG", source="untrusted", payload={"record": record.model_dump(mode="json")})
    with pytest.raises(ValueError, match="cannot establish objection dispositions"):
        replay_decisions([event])


def test_conflicting_objection_binding_is_rejected_before_event_append(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    lifecycle = DecisionLifecycle.create(
        store, DecisionRecord(id="capture", proposal="test", participants={"ENG"}, authority_scopes={"ENG": {"test"}}),
        actor="ENG", source="test",
    )
    original = DecisionPosition(participant="ENG", position=Position.BLOCK, category="safety", rationale="hazard one",
                                objection_id="o", authority_scope="test", applicability_scope="test",
                                applicability_revision=1)
    lifecycle.capture_initial(original, actor="ENG", source="test")
    with pytest.raises(ValueError, match="objection identity.*different position"):
        lifecycle.capture_final(original.model_copy(update={"rationale": "hazard two"}), actor="ENG", source="test")
    assert len(store.events()) == 2
    assert lifecycle.record.final_positions == ()


def test_validated_append_rejects_duplicate_event_identity(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    first = Event(id="duplicate", event_type="observation", payload={"value": "first"})
    store.append_validated(first, lambda history, candidate: None)
    second = Event(id="duplicate", event_type="observation", payload={"value": "second"})
    with pytest.raises(ValueError, match="duplicate event identity"):
        store.append_validated(second, lambda history, candidate: None)
    assert len(store.events()) == 1


def test_validated_append_rejects_candidate_mutation_before_write(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    event = Event(event_type="observation", payload={"value": "original"})

    def mutate(history, candidate):
        candidate.payload["value"] = "changed"

    with pytest.raises(ValueError, match="validator mutated"):
        store.append_validated(event, mutate)
    assert store.events() == []


def _raw_objection(decision_id):
    transition = ObjectionTransition(operation="raised", decision_id=decision_id, objection_id="raw",
        category="safety", claim="unsafe", scope="test", authority_scope="test", resolution_predicate_id="safe")
    return Event(event_type="objection_lifecycle", actor="ENG", source="raw", payload=transition.model_dump(mode="json"))


def test_objection_writer_requires_a_stored_decision(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    claimed = DecisionRecord(id="missing", proposal="test", participants={"ENG"}, authority_scopes={"ENG": {"test"}})
    ledger = ObjectionLifecycle(store, claimed)
    command = RaiseObjectionCommand(objection_id="o", category="safety", claim="unsafe", scope="test",
                                    authority_scope="test", resolution_predicate_id="safe")
    with pytest.raises(KeyError, match="not present"):
        ledger.raise_objection(command, actor="ENG", source="test")
    with pytest.raises(KeyError, match="not present"):
        _ = ledger.projection
    assert store.events() == []


def test_caller_cannot_supply_authority_absent_from_stored_decision(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    stored = DecisionRecord(id="live", proposal="test", participants={"ENG"})
    DecisionLifecycle.create(store, stored, actor="ENG", source="test")
    forged = DecisionRecord(id="live", proposal="test", participants={"ENG"}, authority_scopes={"ENG": {"test"}})
    ledger = ObjectionLifecycle(store, forged)
    command = RaiseObjectionCommand(objection_id="o", category="safety", claim="unsafe", scope="test",
                                    authority_scope="test", resolution_predicate_id="safe")
    with pytest.raises(ValueError, match="authority scope has no participating resolver"):
        ledger.raise_objection(command, actor="ENG", source="test")
    assert len(store.events()) == 1


def test_raw_objection_after_terminal_decision_invalidates_replay(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    lifecycle = DecisionLifecycle.create(
        store, DecisionRecord(id="finished", proposal="test", participants={"ENG"}, authority_scopes={"ENG": {"test"}}),
        actor="ENG", source="test",
    )
    lifecycle.capture_initial(DecisionPosition(participant="ENG", position=Position.SUPPORT), actor="ENG", source="test")
    lifecycle.resolve("consent", "record decision", actor="ENG", source="test")
    store.append(_raw_objection("finished"))
    with pytest.raises(ValueError, match="objection.*terminal"):
        _ = lifecycle.record


def test_raw_objection_before_creation_invalidates_replay(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    store.append(_raw_objection("new"))
    with pytest.raises(ValueError, match="objection.*creation"):
        DecisionLifecycle.create(
            store, DecisionRecord(id="new", proposal="test", participants={"ENG"}, authority_scopes={"ENG": {"test"}}),
            actor="ENG", source="test",
        )
    assert len(store.events()) == 1


def test_raw_objection_from_nonparticipant_invalidates_replay(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    lifecycle = DecisionLifecycle.create(
        store, DecisionRecord(id="live", proposal="test", participants={"ENG"}, authority_scopes={"ENG": {"test"}}),
        actor="ENG", source="test",
    )
    event = _raw_objection("live").model_copy(update={"actor": "outsider"})
    store.append(event)
    with pytest.raises(ValueError, match="actor must be a decision participant"):
        _ = lifecycle.record


def _process_capture(path, barrier, queue):
    lifecycle = DecisionLifecycle(EventStore(path), "race")
    barrier.wait(timeout=10)
    try:
        lifecycle.capture_initial(DecisionPosition(participant="ENG", position=Position.SUPPORT), actor="ENG", source="test")
        queue.put("committed")
    except ValueError as exc:
        queue.put("rejected" if "already captured" in str(exc) else f"unexpected: {exc}")


def test_competing_processes_cannot_both_capture(tmp_path):
    path = str(tmp_path / "events.jsonl")
    DecisionLifecycle.create(
        EventStore(path), DecisionRecord(id="race", proposal="test", participants={"ENG"}),
        actor="ENG", source="test",
    )
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_process_capture, args=(path, barrier, queue)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert sorted(queue.get(timeout=5) for _ in processes) == ["committed", "rejected"]
    assert len(DecisionLifecycle(EventStore(path), "race").record.initial_positions) == 1


def test_competing_initial_captures_cannot_both_commit(tmp_path):
    path = tmp_path / "events.jsonl"
    DecisionLifecycle.create(
        EventStore(path), DecisionRecord(id="race", proposal="test", participants={"ENG"}),
        actor="ENG", source="test",
    )
    barrier = Barrier(2)

    def capture():
        lifecycle = DecisionLifecycle(EventStore(path), "race")
        barrier.wait(timeout=10)
        try:
            lifecycle.capture_initial(
                DecisionPosition(participant="ENG", position=Position.SUPPORT),
                actor="ENG", source="test",
            )
            return "committed"
        except ValueError as exc:
            assert "already captured" in str(exc)
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: capture(), range(2)))
    assert sorted(results) == ["committed", "rejected"]
    assert len(DecisionLifecycle(EventStore(path), "race").record.initial_positions) == 1


def test_objection_cannot_be_added_after_terminal_decision(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    lifecycle = DecisionLifecycle.create(
        store, DecisionRecord(id="finished", proposal="test", participants={"ENG"}, authority_scopes={"ENG": {"test"}}),
        actor="ENG", source="test",
    )
    lifecycle.capture_initial(DecisionPosition(participant="ENG", position=Position.SUPPORT), actor="ENG", source="test")
    stale_snapshot = lifecycle.record
    lifecycle.resolve("consent", "record decision", actor="ENG", source="test")
    ledger = ObjectionLifecycle(store, stale_snapshot)
    with pytest.raises(ValueError, match="terminal decision"):
        ledger.raise_objection(
            RaiseObjectionCommand(
                objection_id="late", category="safety", claim="unsafe", scope="test",
                authority_scope="test", resolution_predicate_id="safe",
            ), actor="ENG", source="test",
        )
    assert len(store.events()) == 3


def test_resolution_and_new_block_cannot_both_commit(tmp_path):
    path = tmp_path / "events.jsonl"
    lifecycle = DecisionLifecycle.create(
        EventStore(path), DecisionRecord(id="race", proposal="test", participants={"ENG"}, authority_scopes={"ENG": {"test"}}),
        actor="ENG", source="test",
    )
    lifecycle.capture_initial(DecisionPosition(participant="ENG", position=Position.SUPPORT), actor="ENG", source="test")
    snapshot = lifecycle.record
    barrier = Barrier(2)

    def resolve():
        current = DecisionLifecycle(EventStore(path), "race")
        barrier.wait(timeout=10)
        try:
            current.resolve("consent", "record decision", actor="ENG", source="test")
            return "resolution committed"
        except ValueError as exc:
            assert "active objection" in str(exc)
            return "resolution rejected"

    def object():
        ledger = ObjectionLifecycle(EventStore(path), snapshot)
        barrier.wait(timeout=10)
        try:
            ledger.raise_objection(
                RaiseObjectionCommand(objection_id="block", category="safety", claim="unsafe",
                                      scope="test", authority_scope="test", resolution_predicate_id="safe"),
                actor="ENG", source="test",
            )
            return "objection committed"
        except ValueError as exc:
            assert "terminal decision" in str(exc)
            return "objection rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = {executor.submit(resolve), executor.submit(object)}
        outcomes = {future.result(timeout=15) for future in results}
    assert outcomes in ({"resolution committed", "objection rejected"},
                        {"resolution rejected", "objection committed"})
    _ = DecisionLifecycle(EventStore(path), "race").record
