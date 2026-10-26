"""Backend-neutral contracts for the bounded experimental current-context slice."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Assertion(Record):
    assertion_id: str = Field(min_length=1)
    proposition_id: str = Field(min_length=1)
    polarity: Literal["positive", "negative"]


class Justification(Record):
    justification_id: str = Field(min_length=1)
    premises: tuple[str, ...] = Field(min_length=1)
    conclusion: str = Field(min_length=1)

    @model_validator(mode="after")
    def distinct_premises(self):
        if len(set(self.premises)) != len(self.premises):
            raise ValueError("justification premises must be distinct")
        return self


class Program(Record):
    schema_version: Literal[1] = 1
    assertions: tuple[Assertion, ...]
    justifications: tuple[Justification, ...] = ()

    @model_validator(mode="after")
    def validate_graph(self):
        assertions = {a.assertion_id: a for a in self.assertions}
        if len(assertions) != len(self.assertions):
            raise ValueError("duplicate assertion identity")
        if len({(a.proposition_id, a.polarity) for a in self.assertions}) != len(
            assertions
        ):
            raise ValueError("a signed proposition must have one assertion identity")
        ids = [j.justification_id for j in self.justifications]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate justification identity")
        edges: dict[str, set[str]] = {key: set() for key in assertions}
        for justification in self.justifications:
            if (
                justification.conclusion not in assertions
                or not set(justification.premises) <= assertions.keys()
            ):
                raise ValueError("justification references undeclared assertion")
            edges[justification.conclusion].update(justification.premises)
        # This slice admits acyclic, explicit signed Horn dependencies only.
        remaining = set(edges)
        while remaining:
            leaves = {key for key in remaining if not edges[key] & remaining}
            if not leaves:
                raise ValueError(
                    "cyclic dependencies are outside the experimental slice"
                )
            remaining -= leaves
        return self


class Change(Record):
    schema_version: Literal[1] = 1
    operation: Literal[
        "add_support",
        "withdraw_support",
        "enable_justification",
        "disable_justification",
    ]
    support_id: str | None = Field(default=None, min_length=1)
    assertion_id: str | None = Field(default=None, min_length=1)
    kind: Literal["evidence", "assumption"] | None = None
    evidence_ref: str | None = Field(default=None, min_length=1)
    justification_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def valid_operation(self):
        fields = self.model_fields_set - {"schema_version", "operation"}
        required = {
            "add_support": {"support_id", "assertion_id", "kind"},
            "withdraw_support": {"support_id"},
            "enable_justification": {"justification_id"},
            "disable_justification": {"justification_id"},
        }[self.operation]
        allowed = required | (
            {"evidence_ref"} if self.operation == "add_support" else set()
        )
        if (
            not required <= fields
            or not fields <= allowed
            or any(getattr(self, key) is None for key in required)
        ):
            raise ValueError("change fields do not match operation")
        if self.kind == "evidence" and self.evidence_ref is None:
            raise ValueError("evidence support requires an evidence reference")
        if self.kind == "assumption" and self.evidence_ref is not None:
            raise ValueError("assumption support cannot carry an evidence reference")
        return self


class Support(Record):
    support_id: str
    assertion_id: str
    kind: Literal["evidence", "assumption"]
    evidence_ref: str | None
    event_id: str


class State(StrEnum):
    SUPPORTED_ONLY = "SUPPORTED_ONLY"
    REFUTED_ONLY = "REFUTED_ONLY"
    BOTH = "BOTH"
    NEITHER = "NEITHER"


class Explanation(Record):
    assertion_id: str
    supported: bool
    direct_supports: tuple[Support, ...] = ()
    justifications: tuple[Justification, ...] = ()
    justification_event_ids: tuple[str, ...] = ()
    # Transitive active evidence/assumption roots, with authoritative provenance.
    roots: tuple[Support, ...] = ()


class Snapshot(Record):
    supported_assertions: tuple[str, ...]
    states: dict[str, State]
    explanations: tuple[Explanation, ...]
