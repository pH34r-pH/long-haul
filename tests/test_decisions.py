from pathlib import Path

import pytest

from long_haul.decisions import DecisionEngine, DecisionRecord, replay_decisions
from long_haul.events import EventStore
from long_haul.models import DecisionPosition, Position


def test_scoped_authority_requires_actual_scope() -> None:
    record = DecisionRecord(
        id="d",
        proposal="p",
        participants={"NAV"},
        authority_scope="routing",
        authority_scopes={"NAV": {"routing"}},
        initial_positions=[DecisionPosition(participant="NAV", position=Position.CONSENT)],
    )
    assert DecisionEngine().resolve(record, "scoped_authority", "act", "NAV").authority_holder == "NAV"
    bad = record.model_copy(update={"authority_scopes": {"NAV": set()}})
    with pytest.raises(ValueError, match="scoped authority"):
        DecisionEngine().resolve(bad, "scoped_authority", "act", "NAV")


def test_late_block_prevents_consent_and_resolved_block_allows_action_preserving_dissent() -> None:
    engine = DecisionEngine()

    # Initial SUPPORT followed by final safety BLOCK must fail consent resolution.
    rec_late_block = DecisionRecord(id="d-late", proposal="deploy", participants={"A", "B"})
    engine.capture_initial(rec_late_block, DecisionPosition(participant="A", position=Position.SUPPORT))
    engine.capture_initial(rec_late_block, DecisionPosition(participant="B", position=Position.SUPPORT))
    engine.record_final(
        rec_late_block,
        DecisionPosition(participant="B", position=Position.BLOCK, category="safety", rationale="new hazard observed"),
    )
    with pytest.raises(ValueError, match="unresolved block"):
        engine.resolve(rec_late_block, "consent", "proceed")

    # Initial safety BLOCK resolved via final SUPPORT with rationale allows consent while retaining initial BLOCK.
    rec_resolved = DecisionRecord(id="d-resolved", proposal="deploy", participants={"A", "B"})
    engine.capture_initial(rec_resolved, DecisionPosition(participant="A", position=Position.SUPPORT))
    engine.capture_initial(
        rec_resolved,
        DecisionPosition(participant="B", position=Position.BLOCK, category="safety", rationale="missing guard"),
    )
    with pytest.raises(ValueError, match="explicit rationale"):
        engine.record_final(rec_resolved, DecisionPosition(participant="B", position=Position.SUPPORT))
    engine.record_final(
        rec_resolved,
        DecisionPosition(participant="B", position=Position.SUPPORT, rationale="guard verified in staging"),
    )
    resolved = engine.resolve(rec_resolved, "consent", "proceed")
    assert resolved.resolution == "consent"
    assert any(p.participant == "B" and p.position is Position.BLOCK for p in resolved.initial_positions)
    assert engine.effective_positions(resolved)["B"].position is Position.SUPPORT


def test_duplicate_unknown_and_mutated_positions_fail_closed() -> None:
    # Duplicate participant with matching count cannot pass coverage guard.
    with pytest.raises(ValueError, match="duplicate participant"):
        DecisionRecord(
            id="dup",
            proposal="x",
            participants={"A", "B"},
            initial_positions=[
                DecisionPosition(participant="A", position=Position.SUPPORT),
                DecisionPosition(participant="A", position=Position.SUPPORT),
            ],
        )

    # Caller-held mutation cannot alter captured history.
    engine = DecisionEngine()
    rec = DecisionRecord(id="immut", proposal="x", participants={"A", "B"})
    pos_a = DecisionPosition(participant="A", position=Position.SUPPORT, evidence=["ev-1"])
    engine.capture_initial(rec, pos_a)
    pos_a.position = Position.BLOCK
    pos_a.evidence.append("tampered")
    assert rec.initial_positions[0].position is Position.SUPPORT
    assert rec.initial_positions[0].evidence == ["ev-1"]


def test_reveal_boundary_enforces_independent_capture_before_disclosure() -> None:
    engine = DecisionEngine()
    rec = DecisionRecord(id="rev", proposal="x", participants={"A", "B"})
    engine.capture_initial(rec, DecisionPosition(participant="A", position=Position.SUPPORT))
    with pytest.raises(ValueError, match="cannot reveal peer positions"):
        engine.reveal_initial_positions(rec)
    engine.capture_initial(rec, DecisionPosition(participant="B", position=Position.CONSENT))
    revealed = engine.reveal_initial_positions(rec)
    assert len(revealed) == 2
    revealed[0].evidence.append("local-mutation")
    assert rec.initial_positions[0].evidence == []


def test_consequential_dispatch_gate_and_event_replay(tmp_path: Path) -> None:
    engine = DecisionEngine()
    store = EventStore(tmp_path / "decisions.jsonl")
    executed: list[str] = []

    rec = DecisionRecord(id="gate-1", proposal="ship release", participants={"NAV-01", "ENG-01"})
    engine.capture_initial(rec, DecisionPosition(participant="NAV-01", position=Position.SUPPORT))
    engine.capture_initial(rec, DecisionPosition(participant="ENG-01", position=Position.CONSENT))

    # Unresolved mandatory decision never invokes the execution adapter.
    with pytest.raises(ValueError, match="unresolved"):
        engine.dispatch_consequential(rec, lambda r: executed.append(r.id))
    assert executed == []

    # After resolution with EventStore provenance, dispatch succeeds and replays identically.
    engine.resolve(rec, "consensus", "ship", event_store=store)
    result = engine.dispatch_consequential(rec, lambda r: executed.append(r.id) or "dispatched")
    assert result == "dispatched"
    assert executed == ["gate-1"]

    replayed = replay_decisions(store.events())
    assert "gate-1" in replayed
    assert replayed["gate-1"].resolution == "consensus"
    assert len(replayed["gate-1"].initial_positions) == 2
