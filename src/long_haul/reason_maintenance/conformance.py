"""Reusable data-driven corpus runner with backend-independent expectations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from ..events.store import Event
from .models import Program, Record, Snapshot, State
from .replay import Backend, replay


class ExpectedExplanation(Record):
    assertion_id: str
    direct_support_ids: tuple[str, ...] = ()
    justification_ids: tuple[str, ...] = ()
    root_support_ids: tuple[str, ...] = ()
    root_event_ids: tuple[str, ...] = ()
    justification_event_ids: tuple[str, ...] = ()


class Checkpoint(Record):
    after: int = Field(ge=0)
    supported_assertions: tuple[str, ...]
    states: dict[str, State]
    explanations: tuple[ExpectedExplanation, ...] = ()


class Case(Record):
    case_id: str
    program: Program
    events: tuple[Event, ...]
    checkpoints: tuple[Checkpoint, ...]

    @model_validator(mode="after")
    def valid_checkpoints(self):
        if not self.checkpoints or any(
            c.after > len(self.events) for c in self.checkpoints
        ):
            raise ValueError("case must contain valid checkpoints")
        if len({c.after for c in self.checkpoints}) != len(self.checkpoints):
            raise ValueError("duplicate checkpoint position")
        return self


class Corpus(Record):
    schema_version: Literal[1] = 1
    simulated: Literal[True] = True
    cases: tuple[Case, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate case identity")
        return self


def load_corpus(path: str | Path) -> Corpus:
    return Corpus.model_validate_json(Path(path).read_text(encoding="utf-8"))


def check_snapshot(actual: Snapshot, expected: Checkpoint) -> None:
    """Compare public semantic identity/provenance, never backend-private IDs."""
    if (
        actual.supported_assertions != expected.supported_assertions
        or actual.states != expected.states
    ):
        raise AssertionError(
            f"semantic mismatch at checkpoint {expected.after}: {actual}"
        )
    by_id = {
        explanation.assertion_id: explanation for explanation in actual.explanations
    }
    for wanted in expected.explanations:
        explanation = by_id[wanted.assertion_id]
        observed = ExpectedExplanation(
            assertion_id=explanation.assertion_id,
            direct_support_ids=tuple(s.support_id for s in explanation.direct_supports),
            justification_ids=tuple(
                j.justification_id for j in explanation.justifications
            ),
            root_support_ids=tuple(s.support_id for s in explanation.roots),
            root_event_ids=tuple(s.event_id for s in explanation.roots),
            justification_event_ids=explanation.justification_event_ids,
        )
        if (
            explanation.supported
            != (wanted.assertion_id in actual.supported_assertions)
            or observed != wanted
        ):
            raise AssertionError(
                f"explanation mismatch at checkpoint {expected.after}: {observed} != {wanted}"
            )


def run_case(case: Case, factory: Callable[[Program], Backend]) -> Snapshot:
    """Check every event prefix with a destroyed/reconstructed backend.

    A separate incremental test can compare snapshots to these normative data.
    This runner can be used by CLIPS, external oracles, and future native adapters.
    """
    for checkpoint in case.checkpoints:
        check_snapshot(
            replay(case.events[: checkpoint.after], factory(case.program)), checkpoint
        )
    return replay(case.events, factory(case.program))
