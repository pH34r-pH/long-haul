from pathlib import Path

import pytest
from pydantic import ValidationError

from long_haul.decisions import DecisionEngine, DecisionRecord, replay_decisions
from long_haul.events import EventStore
from long_haul.models import DecisionPosition, Position


def test_scoped_authority_requires_actual_scope(tmp_path: Path) -> None:
    record = DecisionRecord(
        id="d",
        proposal="p",
        participants={"NAV"},
        authority_scope="routing",
        authority_scopes={"NAV": {"routing"}},
        initial_positions=[DecisionPosition(participant="NAV", position=Position.CONSENT)],
    )
    store = EventStore(tmp_path / "authority.jsonl")
    resolved = DecisionEngine().resolve(record, "scoped_authority", "act", "NAV", event_store=store)
    assert resolved.authority_holder == "NAV"
    assert record.resolution is None
    assert replay_decisions(store.events())["d"] == resolved
    bad = DecisionRecord(id="bad", proposal="p", participants={"NAV"}, authority_scope="routing",
                         authority_scopes={"NAV": set()}, initial_positions=record.initial_positions)
    with pytest.raises(ValueError, match="scoped authority"):
        DecisionEngine().resolve(bad, "scoped_authority", "act", "NAV")


def test_late_block_prevents_consent_and_resolved_block_allows_action_preserving_dissent() -> None:
    engine = DecisionEngine()

    # Initial SUPPORT followed by final safety BLOCK must fail consent resolution.
    rec_late_block = DecisionRecord(id="d-late", proposal="deploy", participants={"A", "B"})
    rec_late_block = engine.capture_initial(rec_late_block, DecisionPosition(participant="A", position=Position.SUPPORT))
    rec_late_block = engine.capture_initial(rec_late_block, DecisionPosition(participant="B", position=Position.SUPPORT))
    rec_late_block = engine.record_final(
        rec_late_block,
        DecisionPosition(participant="B", position=Position.BLOCK, category="safety", rationale="new hazard observed"),
    )
    with pytest.raises(ValueError, match="unresolved block"):
        engine.resolve(rec_late_block, "consent", "proceed")
    with pytest.raises(ValueError, match="explicit rationale"):
        engine.record_final(rec_late_block, DecisionPosition(participant="B", position=Position.SUPPORT))
    with pytest.raises(ValueError, match="explicit rationale"):
        engine.record_final(rec_late_block, DecisionPosition(participant="B", position=Position.SUPPORT, rationale="  "))

    # Initial safety BLOCK resolved via final SUPPORT with rationale allows consent while retaining initial BLOCK.
    rec_resolved = DecisionRecord(id="d-resolved", proposal="deploy", participants={"A", "B"})
    rec_resolved = engine.capture_initial(rec_resolved, DecisionPosition(participant="A", position=Position.SUPPORT))
    rec_resolved = engine.capture_initial(
        rec_resolved,
        DecisionPosition(participant="B", position=Position.BLOCK, category="safety", rationale="missing guard"),
    )
    with pytest.raises(ValueError, match="explicit rationale"):
        engine.record_final(rec_resolved, DecisionPosition(participant="B", position=Position.SUPPORT))
    rec_resolved = engine.record_final(
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
    external_evidence = ["ev-1"]
    pos_a = DecisionPosition(participant="A", position=Position.SUPPORT, evidence=external_evidence)
    previous = rec
    rec = engine.capture_initial(rec, pos_a)
    external_evidence.append("tampered")
    with pytest.raises(ValidationError):
        pos_a.position = Position.BLOCK
    with pytest.raises(AttributeError):
        pos_a.evidence.append("tampered")
    assert previous.initial_positions == ()
    assert rec.initial_positions[0].position is Position.SUPPORT
    assert rec.initial_positions[0].evidence == ("ev-1",)
    with pytest.raises(ValidationError):
        rec.initial_positions[0].position = Position.BLOCK
    with pytest.raises(ValidationError):
        rec.initial_positions = ()
    with pytest.raises(ValidationError):
        rec.participants = frozenset()
    forged_evidence = ["source"]
    unchecked_copy = pos_a.model_copy(update={"evidence": forged_evidence})
    imported = DecisionRecord(id="imported", proposal="x", participants={"A"},
                              initial_positions=[unchecked_copy])
    forged_evidence.append("later mutation")
    assert imported.initial_positions[0].evidence == ("source",)


def test_reveal_boundary_enforces_independent_capture_before_disclosure() -> None:
    engine = DecisionEngine()
    rec = DecisionRecord(id="rev", proposal="x", participants={"A", "B"})
    rec = engine.capture_initial(rec, DecisionPosition(participant="A", position=Position.SUPPORT))
    with pytest.raises(ValueError, match="cannot reveal peer positions"):
        engine.reveal_initial_positions(rec)
    rec = engine.capture_initial(rec, DecisionPosition(participant="B", position=Position.CONSENT))
    revealed = engine.reveal_initial_positions(rec)
    assert len(revealed) == 2
    with pytest.raises(AttributeError):
        revealed[0].evidence.append("local-mutation")
    assert rec.initial_positions[0].evidence == ()


def test_consequential_dispatch_gate_and_event_replay(tmp_path: Path) -> None:
    engine = DecisionEngine()
    store = EventStore(tmp_path / "decisions.jsonl")
    executed: list[str] = []

    rec = DecisionRecord(id="gate-1", proposal="ship release", participants={"NAV-01", "ENG-01"})
    rec = engine.capture_initial(rec, DecisionPosition(participant="NAV-01", position=Position.SUPPORT))
    rec = engine.capture_initial(rec, DecisionPosition(participant="ENG-01", position=Position.CONSENT))

    # Unresolved mandatory decision never invokes the execution adapter.
    with pytest.raises(ValueError, match="not integrated"):
        engine.dispatch_consequential(rec, lambda r: executed.append(r.id))
    assert executed == []

    # A local resolution event is not yet an authorized runtime integration.
    rec = engine.resolve(rec, "consensus", "ship", event_store=store)
    with pytest.raises(ValueError, match="already resolved"):
        engine.resolve(rec, "consent", "ship again")
    with pytest.raises(ValueError, match="not integrated"):
        engine.dispatch_consequential(rec, lambda r: executed.append(r.id))
    assert executed == []

    replayed = replay_decisions(store.events())
    assert "gate-1" in replayed
    assert replayed["gate-1"].resolution == "consensus"
    assert len(replayed["gate-1"].initial_positions) == 2
    assert replayed["gate-1"] == rec


@pytest.mark.parametrize("category,rationale", [(None, "hazard"), ("safety", "   "), ("preference", "no")])
def test_block_requires_typed_category_and_nonblank_rationale(category, rationale) -> None:
    engine = DecisionEngine()
    record = DecisionRecord(id="invalid-block", proposal="x", participants={"A"})
    with pytest.raises(ValueError, match="non-preference typed rationale"):
        engine.capture_initial(record, DecisionPosition(participant="A", position=Position.BLOCK,
                                                        category=category, rationale=rationale))

def test_mutated_or_forged_record_cannot_dispatch_consequential_action() -> None:
    engine = DecisionEngine()
    record = DecisionRecord(
        id="forged", proposal="synthetic", participants={"A"},
        initial_positions=[DecisionPosition(participant="A", position=Position.BLOCK,
                                            category="safety", rationale="hazard")],
    )
    with pytest.raises(ValidationError):
        record.initial_positions[0].position = Position.SUPPORT
    with pytest.raises(ValidationError):
        record.resolution = "consensus"
    # Pydantic's unchecked copy API can still construct an untrusted forged object.
    record = record.model_copy(update={"resolution": "consensus", "action": "synthetic"})
    executed: list[str] = []
    with pytest.raises(ValueError, match="not integrated"):
        engine.dispatch_consequential(record, lambda _: executed.append("invoked"))
    record = record.model_copy(update={"consequential": False})
    with pytest.raises(ValueError, match="not integrated"):
        engine.dispatch_consequential(record, lambda _: executed.append("invoked"))
    assert executed == []
