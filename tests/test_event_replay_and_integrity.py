"""Regression and integrity tests for EventStore replay (#288) and chaining/locking (#289)."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from long_haul.events import (
    ChainCheckpoint,
    Event,
    EventStore,
    ReplayError,
    materialize,
)


def test_torn_tail_returns_valid_prefix_in_inspection_and_recovers_without_mutating_on_read(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    store = EventStore(path)
    first = store.append(Event(event_type="observation", payload={"step": 1}))
    with path.open("ab") as fh:
        fh.write(b'{"event_type":"obser')
    damaged_bytes = path.read_bytes()

    # Inspection surfaces valid prefix + incomplete tail without mutating file.
    replay = store.read_replay(strict=False)
    assert len(replay.events) == 1
    assert replay.events[0].id == first.id
    assert replay.incomplete_tail is not None
    assert replay.incomplete_tail.line == 2
    assert not replay.complete
    assert path.read_bytes() == damaged_bytes

    # Strict replay and ordinary append fail closed on unhandled torn tail.
    with pytest.raises(ReplayError) as strict_exc:
        store.events(strict=True)
    assert strict_exc.value.diagnostic.line == 2
    with pytest.raises(ReplayError):
        store.append(Event(event_type="observation", payload={"step": 2}))

    # Explicit recovery preserves original bytes and allows subsequent appends.
    diag = store.recover_torn_tail()
    assert diag is not None and diag.line == 2
    backup = path.with_suffix(".jsonl.damaged")
    assert backup.read_bytes() == damaged_bytes
    assert store.recover_torn_tail() is None
    second = store.append(Event(event_type="observation", payload={"step": 2}))
    assert [e.id for e in store.events()] == [first.id, second.id]


def test_valid_final_record_without_newline_and_interior_corruption(tmp_path: Path) -> None:
    path = tmp_path / "no-newline.jsonl"
    ev = Event(event_type="observation", payload={"msg": "ok"})
    path.write_text(ev.model_dump_json(), encoding="utf-8")
    store = EventStore(path)
    assert len(store.events()) == 1
    assert store.read_replay().integrity_status == "legacy_unverified"

    # Interior malformed JSON always fails closed (never treated as torn tail).
    interior = tmp_path / "interior.jsonl"
    interior.write_text(ev.model_dump_json() + "\n{bad\n" + ev.model_dump_json() + "\n", encoding="utf-8")
    with pytest.raises(ReplayError) as exc:
        EventStore(interior).read_replay(strict=False)
    assert exc.value.diagnostic.line == 2

    # Complete invalid final record fails closed (not a torn tail).
    complete_bad = tmp_path / "complete-bad.jsonl"
    complete_bad.write_text(ev.model_dump_json() + "\n" + '{"event_type":"observation","timestamp":"not-a-date"}\n', encoding="utf-8")
    with pytest.raises(ReplayError) as exc2:
        EventStore(complete_bad).read_replay(strict=False)
    assert exc2.value.diagnostic.line == 2


def test_unknown_event_type_and_unsupported_schema_version_quarantined_and_strict_fails(tmp_path: Path) -> None:
    path = tmp_path / "future.jsonl"
    store = EventStore(path)
    e1 = store.append(Event(event_type="observation", payload={"msg": "αβγ"}))

    # Append a properly chained future-type record and unsupported schema_version record.
    from long_haul.events.integrity import compute_event_hash

    future_rec = {
        "id": "future-1",
        "timestamp": e1.timestamp.isoformat(),
        "event_type": "future_experimental_event",
        "actor": None,
        "source": None,
        "payload": {"note": "opaque-future-π"},
        "schema_version": 1,
        "log_id": e1.log_id,
        "seq": 2,
        "prev_hash": e1.event_hash,
    }
    future_rec["event_hash"] = compute_event_hash(future_rec)

    v999_rec = {
        "id": "v999-1",
        "timestamp": e1.timestamp.isoformat(),
        "event_type": "observation",
        "actor": None,
        "source": None,
        "payload": {},
        "schema_version": 999,
        "log_id": e1.log_id,
        "seq": 3,
        "prev_hash": future_rec["event_hash"],
    }
    v999_rec["event_hash"] = compute_event_hash(v999_rec)

    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(future_rec, ensure_ascii=False) + "\n")
        fh.write(json.dumps(v999_rec, ensure_ascii=False) + "\n")

    replay = store.read_replay(strict=False)
    assert len(replay.events) == 1
    assert len(replay.quarantined) == 2
    assert not replay.complete
    # Hash chain remains verifiable across opaque future envelopes.
    assert replay.integrity_status == "locally_consistent_unanchored"
    assert replay.checkpoint is not None and replay.checkpoint.seq == 3
    exported = store.export_jsonl()
    assert "future_experimental_event" in exported and "999" in exported

    with pytest.raises(ReplayError):
        store.events(strict=True)


def test_missing_crew_and_bad_schema_version_raise_typed_replay_error() -> None:
    with pytest.raises(ReplayError, match="unknown crew member"):
        materialize([Event(event_type="competence_update", payload={"crew_id": "missing", "competence": []})])
    with pytest.raises(ReplayError, match="unknown crew member"):
        materialize([Event(event_type="embodiment_change", payload={"crew_id": "missing", "embodiment": {}})])
    with pytest.raises(ReplayError, match="unsupported schema_version"):
        materialize([Event(event_type="observation", schema_version=999)])


def test_hash_chain_detects_deletion_edit_reordering_and_anchor_rollback(tmp_path: Path) -> None:
    path = tmp_path / "chained.jsonl"
    store = EventStore(path)
    e1 = store.append(Event(event_type="observation", payload={"n": 1}))
    e2 = store.append(Event(event_type="observation", payload={"n": 2}))
    e3 = store.append(Event(event_type="observation", payload={"n": 3}))
    assert (e1.seq, e2.seq, e3.seq) == (1, 2, 3)

    anchor = store.checkpoint()
    assert anchor is not None and anchor.seq == 3
    verified = store.verify_chain(anchor=anchor)
    assert verified.integrity_status == "verified_against_anchor"
    assert verified.complete

    lines = path.read_text(encoding="utf-8").splitlines()

    # Interior deletion detected
    deleted = tmp_path / "deleted.jsonl"
    deleted.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    with pytest.raises(ReplayError) as del_exc:
        EventStore(deleted).verify_chain()
    assert del_exc.value.diagnostic.line == 2

    # Interior edit detected
    edited_rec = json.loads(lines[1])
    edited_rec["payload"]["n"] = 999
    edited = tmp_path / "edited.jsonl"
    edited.write_text("\n".join([lines[0], json.dumps(edited_rec), lines[2]]) + "\n", encoding="utf-8")
    with pytest.raises(ReplayError) as edit_exc:
        EventStore(edited).verify_chain()
    assert edit_exc.value.diagnostic.line == 2

    # Reordering detected
    reordered = tmp_path / "reordered.jsonl"
    reordered.write_text("\n".join([lines[1], lines[0], lines[2]]) + "\n", encoding="utf-8")
    with pytest.raises(ReplayError):
        EventStore(reordered).verify_chain()

    # Truncation/rollback detected against trusted anchor
    truncated = tmp_path / "truncated.jsonl"
    truncated.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    assert EventStore(truncated).verify_chain().integrity_status == "locally_consistent_unanchored"
    with pytest.raises(ReplayError, match="truncated/rolled back"):
        EventStore(truncated).verify_chain(anchor=anchor)

    # Full history rewrite with recomputed hashes detected against trusted anchor
    rewritten = tmp_path / "rewritten.jsonl"
    rw_store = EventStore(rewritten)
    for idx in range(3):
        rw_store.append(Event(event_type="observation", payload={"n": idx + 10}))
    assert rw_store.verify_chain().integrity_status == "locally_consistent_unanchored"
    with pytest.raises(ReplayError, match="anchor"):
        rw_store.verify_chain(anchor=anchor)
    wrong_hash_anchor = ChainCheckpoint(log_id=anchor.log_id, seq=2, head_hash="0" * 64, event_count=2)
    with pytest.raises(ReplayError, match="anchor hash mismatch"):
        store.verify_chain(anchor=wrong_hash_anchor)


def test_concurrent_thread_appends_produce_strictly_sequential_chain(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.jsonl"
    store = EventStore(path)

    def write_one(i: int) -> int | None:
        ev = store.append(Event(event_type="observation", payload={"worker": i}))
        return ev.seq

    with ThreadPoolExecutor(max_workers=6) as pool:
        seqs = list(pool.map(write_one, range(24)))

    assert sorted(s for s in seqs if s is not None) == list(range(1, 25))
    replay = store.verify_chain()
    assert replay.complete
    assert replay.checkpoint is not None and replay.checkpoint.seq == 24
