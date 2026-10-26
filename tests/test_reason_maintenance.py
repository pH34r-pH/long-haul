"""Normative corpus, replay, validation and optional native adapter checks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from long_haul.events.store import Event, EventStore
from long_haul.reason_maintenance import (
    Assertion,
    Change,
    Justification,
    Program,
    State,
    replay,
)
from long_haul.reason_maintenance.conformance import (
    check_snapshot,
    load_corpus,
    run_case,
)
from long_haul.reason_maintenance.replay import NAMESPACE

CORPUS = Path(__file__).parent / "fixtures/reason_maintenance/corpus-v1.json"
CASES = load_corpus(CORPUS).cases


@pytest.fixture
def factory():
    # General runtime tests require no optional backend. The dedicated CI job
    # requires it, so packaging/version errors cannot become passing skips.
    if not os.environ.get("LONG_HAUL_REQUIRE_CLIPS"):
        pytest.importorskip("clips")
    from long_haul.reason_maintenance.clips_backend import ClipsBackend

    return ClipsBackend


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
def test_corpus_and_incremental_prefixes(case, factory):
    rebuilt = run_case(case, factory)
    incremental = factory(case.program)
    checkpoints = {checkpoint.after: checkpoint for checkpoint in case.checkpoints}
    if 0 in checkpoints:
        check_snapshot(incremental.snapshot(), checkpoints[0])
    for position, event in enumerate(case.events, 1):
        incremental.apply(Change.model_validate(event.payload[NAMESPACE]), event.id)
        if position in checkpoints:
            check_snapshot(incremental.snapshot(), checkpoints[position])
    assert incremental.snapshot() == rebuilt


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
def test_authoritative_store_and_process_restart(case, factory, tmp_path):
    store = EventStore(tmp_path / "authoritative.jsonl")
    for event in case.events:
        store.append(event)
    # Authoritative bytes stay unchanged while backends are discarded.
    before = store.path.read_bytes()
    expected = replay(store.events(), factory(case.program))
    program_path = tmp_path / "program.json"
    program_path.write_text(case.program.model_dump_json())
    code = """
import sys
from pathlib import Path
from long_haul.events.store import EventStore
from long_haul.reason_maintenance import Program, replay
from long_haul.reason_maintenance.clips_backend import ClipsBackend
program = Program.model_validate_json(Path(sys.argv[1]).read_text())
print(replay(EventStore(sys.argv[2]).events(), ClipsBackend(program)).model_dump_json())
"""
    outputs = [
        subprocess.check_output(
            [sys.executable, "-c", code, str(program_path), str(store.path)],
            text=True,
        ).strip()
        for _ in range(2)
    ]
    assert outputs[0] == outputs[1] == expected.model_dump_json()
    assert store.path.read_bytes() == before


def test_corpus_is_backend_neutral_and_synthetic():
    corpus = load_corpus(CORPUS)
    assert corpus.simulated is True
    text = CORPUS.read_text()
    assert "defrule" not in text and "fact-index" not in text
    assert len(corpus.cases) == 4


@pytest.mark.parametrize(
    "change",
    [
        {
            "operation": "add_support",
            "support_id": "s",
            "assertion_id": "A",
            "kind": "evidence",
        },
        {"operation": "withdraw_support", "support_id": "s", "assertion_id": "A"},
        {"operation": "enable_justification", "justification_id": None},
        {
            "operation": "add_support",
            "support_id": "s",
            "assertion_id": "A",
            "kind": "assumption",
            "evidence_ref": "ref",
        },
        {"schema_version": 2, "operation": "withdraw_support", "support_id": "s"},
    ],
)
def test_malformed_changes_fail(change):
    with pytest.raises(ValidationError):
        Change.model_validate(change)


@pytest.mark.parametrize(
    "program",
    [
        {
            "assertions": [
                {"assertion_id": "A", "proposition_id": "P", "polarity": "positive"}
            ]
            * 2
        },
        {
            "assertions": [
                {"assertion_id": "A", "proposition_id": "P", "polarity": "positive"},
                {"assertion_id": "B", "proposition_id": "P", "polarity": "positive"},
            ]
        },
        {
            "assertions": [
                {"assertion_id": "A", "proposition_id": "P", "polarity": "positive"}
            ],
            "justifications": [
                {"justification_id": "r", "premises": ["missing"], "conclusion": "A"}
            ],
        },
        {
            "assertions": [
                {"assertion_id": "A", "proposition_id": "P", "polarity": "positive"}
            ],
            "justifications": [
                {"justification_id": "r", "premises": ["A"], "conclusion": "A"}
            ],
        },
    ],
)
def test_invalid_or_out_of_scope_programs_fail(program):
    with pytest.raises(ValidationError):
        Program.model_validate(program)


def test_invalid_transitions_leave_semantics_unchanged(factory):
    case = CASES[0]
    backend = factory(case.program)
    backend.apply(
        Change.model_validate(case.events[0].payload[NAMESPACE]), case.events[0].id
    )
    before = backend.snapshot()
    changes = [
        (
            Change(
                operation="add_support",
                support_id="evidence:one",
                assertion_id="P",
                kind="assumption",
            ),
            "new:1",
        ),
        (
            Change(
                operation="add_support",
                support_id="new",
                assertion_id="missing",
                kind="assumption",
            ),
            "new:2",
        ),
        (Change(operation="withdraw_support", support_id="missing"), "new:3"),
        (Change(operation="enable_justification", justification_id="missing"), "new:4"),
        (
            Change(operation="disable_justification", justification_id="missing"),
            "new:5",
        ),
        (
            Change(operation="withdraw_support", support_id="evidence:one"),
            case.events[0].id,
        ),
    ]
    for change, identity in changes:
        with pytest.raises(ValueError):
            backend.apply(change, identity)
        assert backend.snapshot() == before
    # A withdrawn ID cannot silently become different evidence on replay.
    backend.apply(
        Change(operation="withdraw_support", support_id="evidence:one"), "withdraw"
    )
    with pytest.raises(ValueError, match="reused"):
        backend.apply(changes[0][0], "fresh")


def test_envelope_and_duplicate_event_admission(factory):
    program = CASES[0].program
    unrelated = Event(id="unrelated", event_type="decision", payload={"other": True})
    assert replay([unrelated], factory(program)).states == {"P": State.NEITHER}
    original = CASES[0].events[0]
    for history in (
        [original, original],
        [unrelated, unrelated],
        [original.model_copy(update={"event_type": "decision"})],
    ):
        with pytest.raises(ValueError):
            replay(history, factory(program))


def test_domain_identities_are_not_clips_syntax(factory):
    identity = 'urn:fixture:quoted" ) (assert (injected)):雪'
    program = Program(
        assertions=(
            Assertion(assertion_id=identity, proposition_id="P", polarity="positive"),
        )
    )
    backend = factory(program)
    backend.apply(
        Change(
            operation="add_support",
            support_id=identity,
            assertion_id=identity,
            kind="assumption",
        ),
        "event:unicode",
    )
    snapshot = backend.snapshot()
    assert snapshot.supported_assertions == (identity,)
    assert snapshot.explanations[0].roots[0].support_id == identity
    assert "fact-index" not in snapshot.model_dump_json()


def test_exact_version_guard(factory, monkeypatch):
    from long_haul.reason_maintenance import clips_backend

    assert clips_backend.compatibility() == {"binding": "1.0.6", "core": "6.4.2"}
    monkeypatch.setattr(clips_backend, "version", lambda _: "1.0.5")
    with pytest.raises(clips_backend.CompatibilityError, match="1.0.5"):
        factory(CASES[0].program)


def test_unidentified_core_rejected(factory, monkeypatch, tmp_path):
    from long_haul.reason_maintenance import clips_backend

    native = tmp_path / "unknown.so"
    native.write_bytes(b"unidentified CLIPS build")

    class UnknownNative:
        __file__ = str(native)

    monkeypatch.setattr(
        clips_backend.importlib, "import_module", lambda _: UnknownNative
    )
    with pytest.raises(clips_backend.CompatibilityError, match="unidentified"):
        clips_backend.compatibility()


def test_conjunctive_premises_are_all_required(factory):
    # The normative conjunction fixture also checks an independent direct root.
    program = Program(
        assertions=tuple(
            Assertion(assertion_id=n, proposition_id=n, polarity="positive")
            for n in "ABC"
        ),
        justifications=(
            Justification(justification_id="both", premises=("A", "B"), conclusion="C"),
        ),
    )
    backend = factory(program)
    backend.apply(
        Change(operation="enable_justification", justification_id="both"), "enable"
    )
    backend.apply(
        Change(
            operation="add_support", support_id="a", assertion_id="A", kind="assumption"
        ),
        "root",
    )
    assert backend.explain("C").supported is False
    with pytest.raises(ValueError):
        backend.explain("undeclared")
    assert json.loads(backend.snapshot().model_dump_json())["states"]["C"] == "NEITHER"
