"""Optional CLIPSpy 1.0.6 / CLIPS 6.4.2 adapter.

CLIPS logical conditional elements own support/retraction. Python maps stable
identities, validates operations, and renders active provenance; it is not a TMS.
"""

from __future__ import annotations

import hashlib
import importlib
import re
from importlib.metadata import version
from pathlib import Path
from typing import Any

from .models import Change, Explanation, Program, Snapshot, State, Support


class CompatibilityError(RuntimeError):
    """The installed backend does not identify as the evaluated exact version."""


def compatibility() -> dict[str, str]:
    """Fail closed on other bindings/core builds, including unidentified builds.

    CLIPSpy exposes no core-version query. This build check reads the native
    extension's embedded vendor banner; it is not a general ABI guarantee.
    """
    try:
        binding = version("clipspy")
        native = importlib.import_module("clips._clips")
    except (ImportError, OSError) as error:
        raise CompatibilityError(
            "install optional prototype dependency clipspy==1.0.6"
        ) from error
    banner = re.search(
        rb"CLIPS \(([0-9]+\.[0-9]+\.[0-9]+) ([0-9/]+)\)",
        Path(native.__file__).read_bytes(),
    )
    core = banner.group(1).decode() if banner else "unidentified"
    if binding != "1.0.6" or core != "6.4.2":
        raise CompatibilityError(
            f"requires clipspy 1.0.6 / CLIPS 6.4.2; found {binding} / {core}"
        )
    return {"binding": binding, "core": core}


def _key(identity: str) -> str:
    # Symbols are private safe tokens; user identities are never CLIPS syntax.
    return "k" + hashlib.sha256(identity.encode()).hexdigest()


class ClipsBackend:
    def __init__(self, program: Program):
        self.versions = compatibility()
        clips = importlib.import_module("clips")
        self._environment = clips.Environment()
        self._program = program
        self._assertions = {a.assertion_id: a for a in program.assertions}
        self._keys = {_key(key): key for key in self._assertions}
        self._justifications = {j.justification_id: j for j in program.justifications}
        self._supports: dict[str, Support] = {}
        self._support_facts: dict[str, Any] = {}
        self._enabled: dict[str, str] = {}
        self._enabled_facts: dict[str, Any] = {}
        self._seen_supports: set[str] = set()
        self._seen_events: set[str] = set()
        for definition in (
            "(deftemplate root (slot support) (slot assertion))",
            "(deftemplate enabled (slot justification))",
            "(deftemplate claim (slot assertion))",
            "(deftemplate derivation (slot justification))",
            "(defrule root-claim (logical (root (assertion ?a))) => (assert (claim (assertion ?a))))",
        ):
            self._environment.build(definition)
        for justification in program.justifications:
            key = _key(justification.justification_id)
            premises = " ".join(
                f"(claim (assertion {_key(p)}))" for p in justification.premises
            )
            self._environment.build(
                f"(defrule rule-{key} (logical (enabled (justification {key})) {premises}) => "
                f"(assert (claim (assertion {_key(justification.conclusion)}))) "
                f"(assert (derivation (justification {key}))))"
            )

    def apply(self, change: Change, event_id: str) -> None:
        """Apply one validated event and run to quiescence before observation."""
        if not event_id or event_id in self._seen_events:
            raise ValueError("empty or duplicate event identity")
        operation = change.operation
        if operation == "add_support":
            if change.assertion_id not in self._assertions:
                raise ValueError("support references undeclared assertion")
            if change.support_id in self._seen_supports:
                raise ValueError("support identity cannot be reused")
            support = Support(
                support_id=change.support_id,
                assertion_id=change.assertion_id,
                kind=change.kind,
                evidence_ref=change.evidence_ref,
                event_id=event_id,
            )
            fact = self._environment.find_template("root").assert_fact(
                support=self._symbol(_key(support.support_id)),
                assertion=self._symbol(_key(support.assertion_id)),
            )
            self._supports[support.support_id] = support
            self._support_facts[support.support_id] = fact
            self._seen_supports.add(support.support_id)
        elif operation == "withdraw_support":
            if change.support_id not in self._support_facts:
                raise ValueError("cannot withdraw inactive support")
            self._support_facts.pop(change.support_id).retract()
            del self._supports[change.support_id]
        elif operation == "enable_justification":
            if change.justification_id not in self._justifications:
                raise ValueError("undeclared justification")
            if change.justification_id in self._enabled:
                raise ValueError("justification already enabled")
            fact = self._environment.find_template("enabled").assert_fact(
                justification=self._symbol(_key(change.justification_id)),
            )
            self._enabled_facts[change.justification_id] = fact
            self._enabled[change.justification_id] = event_id
        else:
            if change.justification_id not in self._enabled:
                raise ValueError("cannot disable inactive justification")
            self._enabled_facts.pop(change.justification_id).retract()
            del self._enabled[change.justification_id]
        self._environment.run()
        self._seen_events.add(event_id)

    @staticmethod
    def _symbol(value: str):
        return importlib.import_module("clips").Symbol(value)

    def _active(self) -> tuple[set[str], set[str]]:
        claims: set[str] = set()
        derivations: set[str] = set()
        rule_keys = {_key(key): key for key in self._justifications}
        for fact in self._environment.facts():
            if fact.template.name == "claim":
                claims.add(self._keys[str(fact["assertion"])])
            elif fact.template.name == "derivation":
                derivations.add(rule_keys[str(fact["justification"])])
        return claims, derivations

    def explain(self, assertion_id: str) -> Explanation:
        if assertion_id not in self._assertions:
            raise ValueError("undeclared assertion")
        claims, derivations = self._active()
        direct = tuple(
            sorted(
                (s for s in self._supports.values() if s.assertion_id == assertion_id),
                key=lambda s: s.support_id,
            )
        )
        justifications = tuple(
            sorted(
                (
                    self._justifications[key]
                    for key in derivations
                    if self._justifications[key].conclusion == assertion_id
                ),
                key=lambda j: j.justification_id,
            )
        )
        # Walk only derivations reported active by CLIPS; no inference here.
        reachable = {assertion_id}
        pending = [assertion_id]
        while pending:
            current = pending.pop()
            for key in sorted(derivations):
                justification = self._justifications[key]
                if justification.conclusion == current:
                    for premise in justification.premises:
                        if premise not in reachable:
                            reachable.add(premise)
                            pending.append(premise)
        roots = tuple(
            sorted(
                (s for s in self._supports.values() if s.assertion_id in reachable),
                key=lambda s: s.support_id,
            )
        )
        return Explanation(
            assertion_id=assertion_id,
            supported=assertion_id in claims,
            direct_supports=direct,
            justifications=justifications,
            justification_event_ids=tuple(
                self._enabled[j.justification_id] for j in justifications
            ),
            roots=roots,
        )

    def snapshot(self) -> Snapshot:
        claims, _ = self._active()
        states = {}
        for proposition in sorted({a.proposition_id for a in self._program.assertions}):
            positive = any(
                a.assertion_id in claims and a.polarity == "positive"
                for a in self._program.assertions
                if a.proposition_id == proposition
            )
            negative = any(
                a.assertion_id in claims and a.polarity == "negative"
                for a in self._program.assertions
                if a.proposition_id == proposition
            )
            states[proposition] = {
                (True, False): State.SUPPORTED_ONLY,
                (False, True): State.REFUTED_ONLY,
                (True, True): State.BOTH,
                (False, False): State.NEITHER,
            }[positive, negative]
        return Snapshot(
            supported_assertions=tuple(sorted(claims)),
            states=states,
            explanations=tuple(self.explain(key) for key in sorted(self._assertions)),
        )
