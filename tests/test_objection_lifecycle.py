import pytest
from pydantic import ValidationError

from long_haul.decisions import (
    AssessObjectionCommand,
    DecisionEngine,
    DecisionLifecycle,
    DecisionLifecycleEvent,
    DecisionRecord,
    NarrowObjectionCommand,
    ObjectionLifecycle,
    RaiseObjectionCommand,
    ReopenObjectionCommand,
    materialize_decision,
    materialize_objections,
)
from long_haul.events import Event, EventStore
from long_haul.models import DecisionPosition, Position


def decision(with_initial_position: bool = True) -> DecisionRecord:
    return DecisionRecord(
        id="decision-1",
        proposal="select an eligible runtime",
        participants={"NAV", "ENG"},
        consequential=False,
        authority_scopes={"NAV": {"runtime"}},
        initial_positions=(
            DecisionPosition(
                participant="ENG",
                position=Position.OBJECT,
                category="factual",
                rationale="runtime-profile evidence is stale",
                objection_id="obj-1",
                authority_scope="runtime",
                applicability_scope="runtime-profile compatibility",
                applicability_revision=1,
            ),
        ) if with_initial_position else (),
    )


@pytest.mark.parametrize(
    ("operation", "extra_field"),
    (
        ("created", "action"),
        ("created", "authority_holder"),
        ("initial_captured", "action"),
        ("initial_captured", "authority_holder"),
    ),
)
def test_lifecycle_event_rejects_present_empty_extra_payload(operation, extra_field):
    fields = {"record": decision(with_initial_position=False)} if operation == "created" else {
        "position": DecisionPosition(participant="ENG", position=Position.SUPPORT)
    }

    with pytest.raises(ValidationError):
        DecisionLifecycleEvent(
            operation=operation,
            decision_id="decision-1",
            **fields,
            **{extra_field: ""},
        )


def raise_objection(ledger: ObjectionLifecycle, objection_id: str = "obj-1"):
    return ledger.raise_objection(
        RaiseObjectionCommand(
            objection_id=objection_id,
            category="factual",
            claim="profile has not been validated for this artifact",
            scope="runtime-profile compatibility",
            authority_scope="runtime",
            resolution_predicate_id="runtime-profile-compatible",
            evidence_refs=("profile-validation:old",),
        ),
        actor="ENG",
        source="initial-position:ENG",
    )


def add_objection(ledger: ObjectionLifecycle, **fields):
    actor = fields.pop("actor")
    source = fields.pop("source")
    return ledger.raise_objection(RaiseObjectionCommand(**fields), actor=actor, source=source)


def reopen_objection(ledger: ObjectionLifecycle, **fields):
    actor = fields.pop("actor")
    source = fields.pop("source")
    return ledger.reopen(ReopenObjectionCommand(**fields), actor=actor, source=source)


def assess_objection(ledger: ObjectionLifecycle, **fields):
    actor = fields.pop("actor")
    source = fields.pop("source")
    return ledger.assess_evidence(AssessObjectionCommand(**fields), actor=actor, source=source)


def narrow_objection(ledger: ObjectionLifecycle, **fields):
    actor = fields.pop("actor")
    source = fields.pop("source")
    return ledger.narrow(NarrowObjectionCommand(**fields), actor=actor, source=source)


def supersession_case(tmp_path):
    record = decision()
    store = EventStore(tmp_path / "events.jsonl")
    ledger = ObjectionLifecycle(store, record)
    raise_objection(ledger, "old")
    add_objection(
        ledger,
        objection_id="new",
        actor="ENG",
        source="review:new-claim",
        category="factual",
        claim="new profile has missing validation",
        scope="new profile only",
        authority_scope="runtime",
        resolution_predicate_id="new-profile-valid",
    )
    return record, store, ledger


def test_replay_keeps_irrelevant_evidence_active_and_resolves_declared_predicate(tmp_path):
    record = decision()
    record = DecisionEngine().capture_final(
        record,
        DecisionPosition(participant="ENG", position=Position.SUPPORT, category="factual", rationale="revised view"),
    )
    store = EventStore(tmp_path / "events.jsonl")
    ledger = ObjectionLifecycle(store, record)
    raised = raise_objection(ledger)
    assert len(raised.active_for_decision()) == 1
    assert raised.active_for_decision(include_live_view=False) == ()
    assert raised.metrics.active_count == 1
    assert raised.metrics.view_bytes > 0
    assert raised.metrics.lifecycle_events == 1
    assert ledger.last_update_elapsed_ns is not None and ledger.last_update_elapsed_ns > 0

    with pytest.raises(ValueError, match="active objection"):
        DecisionEngine().resolve_with_events(record, "consent", "dispatch", store.events())
    assert ledger.projection.active_for_decision(include_live_view=False) == ()
    with pytest.raises(ValueError, match="active objection"):
        DecisionEngine().resolve_with_events(record, "consent", "dispatch", store.events())

    unrelated = assess_objection(
        ledger,
        objection_id="obj-1",
        actor="ENG",
        source="observation:unrelated",
        expected_revision=1,
        evidence_ref="host-health:healthy",
        predicate_id="host-health-current",
        outcome="satisfied",
    )
    assert unrelated.get("obj-1").status == "active"

    unknown = assess_objection(
        ledger,
        objection_id="obj-1",
        actor="NAV",
        source="profile-validation:incomplete",
        expected_revision=2,
        evidence_ref="profile-validation:incomplete",
        predicate_id="runtime-profile-compatible",
        outcome="unknown",
    )
    assert unknown.get("obj-1").status == "active"

    resolved = assess_objection(
        ledger,
        objection_id="obj-1",
        actor="NAV",
        source="profile-validation:current",
        expected_revision=3,
        evidence_ref="profile-validation:current",
        predicate_id="runtime-profile-compatible",
        outcome="satisfied",
    )
    assert resolved.get("obj-1").status == "resolved"
    assert resolved.get("obj-1").evidence_refs == (
        "profile-validation:old",
        "host-health:healthy",
        "profile-validation:incomplete",
        "profile-validation:current",
    )
    rebuilt = materialize_objections(EventStore(store.path).events(), record)
    assert rebuilt.objections == resolved.objections
    assert rebuilt.metrics.events_scanned == resolved.metrics.events_scanned
    assert rebuilt.metrics.serialized_event_bytes == resolved.metrics.serialized_event_bytes
    final = DecisionEngine().resolve_with_events(record, "consent", "dispatch", store.events())
    assert final.action == "dispatch"
    assert final.initial_positions[0].position is Position.OBJECT
    assert final.final_positions[0].position is Position.SUPPORT
    assert DecisionRecord.model_validate(final.model_dump()) == final


def test_position_link_binds_category_authority_and_applicability_scope(tmp_path):
    record = decision()
    mismatch_cases = (
        ({"category": "safety"}, {}, "category differs"),
        ({}, {"authority_scope": "other"}, "authority scope differs"),
        ({"scope": "another applicability"}, {}, "applicability scope differs"),
    )
    for index, (objection_override, position_override, message) in enumerate(mismatch_cases):
        store = EventStore(tmp_path / f"mismatch-{index}.jsonl")
        position = record.initial_positions[0].model_copy(update=position_override)
        mismatched_record = record.model_copy(update={"initial_positions": (position,)})
        ledger = ObjectionLifecycle(store, mismatched_record)
        fields = {
            "category": "factual",
            "claim": "runtime profile may be stale",
            "scope": "runtime-profile compatibility",
            "authority_scope": "runtime",
            "resolution_predicate_id": "runtime-profile-compatible",
            "evidence_refs": ("profile-validation:old",),
        }
        fields.update(objection_override)
        add_objection(
            ledger,
            objection_id="obj-1",
            actor="ENG",
            source="initial-position:ENG",
            **fields,
        )
        with pytest.raises(ValueError, match=message):
            DecisionEngine().resolve_with_events(
                mismatched_record, "recorded_disagreement", "dispatch with dissent", store.events()
            )


def test_plain_resolution_ignores_caller_supplied_resolved_objection_ids():
    forged = decision().model_copy(update={"resolved_objection_ids": ("obj-1",)})
    with pytest.raises(ValueError, match="explicit resolution mode"):
        DecisionEngine().resolve(forged, "consent", "dispatch")
    experimental = DecisionEngine().resolve(forged, "experiment", "verify the objection")
    assert experimental.resolved_objection_ids == ()


def test_resource_block_survives_successor_chain_and_reopening(tmp_path):
    record = DecisionRecord(
        id="blocked-runtime",
        proposal="select a runtime",
        participants={"NAV", "ENG"},
        consequential=False,
        authority_scopes={"NAV": {"runtime"}},
        initial_positions=(
            DecisionPosition(
                participant="ENG",
                position=Position.BLOCK,
                category="resource",
                rationale="the selected runtime has unresolved capacity evidence",
                objection_id="old",
                authority_scope="runtime",
                applicability_scope="runtime-profile compatibility",
                applicability_revision=1,
            ),
        ),
    )
    store = EventStore(tmp_path / "events.jsonl")
    ledger = ObjectionLifecycle(store, record)
    for objection_id, claim, scope in (
        ("old", "runtime capacity is unknown", "runtime-profile compatibility"),
        ("new", "selected profile capacity is unknown", "selected profile"),
        ("latest", "current placement capacity is unknown", "current placement"),
    ):
        add_objection(
            ledger,
            objection_id=objection_id,
            actor="ENG",
            source=f"initial-objection:{objection_id}",
            category="resource",
            claim=claim,
            scope=scope,
            authority_scope="runtime",
            resolution_predicate_id=f"{objection_id}-capacity-verified",
        )
    ledger.supersede(
        "old", actor="NAV", source="review:narrow-1", expected_revision=1,
        successor_id="new", reason="replace with the selected-profile claim",
    )
    ledger.supersede(
        "new", actor="NAV", source="review:narrow-2", expected_revision=1,
        successor_id="latest", reason="replace with the current-placement claim",
    )
    with pytest.raises(ValueError, match="unresolved block"):
        DecisionEngine().resolve_with_events(
            record, "recorded_disagreement", "dispatch despite active resource block", store.events()
        )

    ledger.resolve(
        "latest", actor="NAV", source="capacity-check:verified", expected_revision=1,
        reason="the declared current-placement capacity predicate was verified",
    )
    settled = DecisionEngine().resolve_with_events(record, "consent", "dispatch", store.events())
    assert settled.resolved_objection_ids == ("latest", "new", "old")
    assert DecisionRecord.model_validate(settled.model_dump()) == settled

    reopen_objection(
        ledger, objection_id="latest", actor="NAV", source="placement-change:new-evidence", expected_revision=2,
        scope="new placement after resource topology changed",
        resolution_predicate_id="latest-capacity-after-topology-change",
        evidence_ref="topology-change:1",
        reason="the previous capacity evidence no longer applies",
    )
    with pytest.raises(ValueError, match="unresolved block"):
        DecisionEngine().resolve_with_events(
            record, "recorded_disagreement", "dispatch despite the reopened block", store.events()
        )
    with pytest.raises(ValueError, match="active objection"):
        DecisionEngine().resolve_with_events(record, "consent", "dispatch", store.events())


def test_objection_scope_requires_a_participating_resolver(tmp_path):
    record = DecisionRecord(
        id="unresolvable-scope",
        proposal="select a runtime",
        participants={"ENG"},
        consequential=False,
        authority_scopes={"NAV": {"runtime"}},
    )
    ledger = ObjectionLifecycle(EventStore(tmp_path / "events.jsonl"), record)
    with pytest.raises(ValueError, match="participating resolver"):
        add_objection(
            ledger,
            objection_id="unresolvable",
            actor="ENG",
            source="initial-objection:ENG",
            category="resource",
            claim="runtime capacity is unknown",
            scope="selected profile",
            authority_scope="runtime",
            resolution_predicate_id="capacity-verified",
        )


def test_authorized_dismissal_is_retained_and_reopen_requires_new_applicability(tmp_path):
    record = decision()
    store = EventStore(tmp_path / "events.jsonl")
    ledger = ObjectionLifecycle(store, record)
    raise_objection(ledger)

    with pytest.raises(ValueError, match="scoped authority"):
        ledger.dismiss(
            "obj-1",
            actor="ENG",
            source="dismissal:unauthorized",
            expected_revision=1,
            reason="not within role scope",
        )
    assert len(store.events()) == 1

    dismissed = ledger.dismiss(
        "obj-1",
        actor="NAV",
        source="decision:authorized-review",
        expected_revision=1,
        reason="the objection used an inapplicable runtime profile record",
        evidence_refs=("profile-validation:wrong-artifact",),
    )
    assert dismissed.get("obj-1").status == "dismissed"

    reopened = reopen_objection(
        ledger,
        objection_id="obj-1",
        actor="NAV",
        source="profile-change:new-runtime",
        expected_revision=2,
        scope="new runtime after backend change",
        resolution_predicate_id="new-runtime-profile-compatible",
        evidence_ref="runtime-change:1",
        reason="applicability changed after the backend replacement",
    )
    objection = reopened.get("obj-1")
    assert objection.status == "active"
    assert objection.applicability_revision == 2
    assert objection.revision == 3
    assert objection.evidence_refs[-1] == "runtime-change:1"
    stale_assessment = assess_objection(
        ledger,
        objection_id="obj-1",
        actor="NAV",
        source="profile-validation:old-predicate-after-reopen",
        expected_revision=3,
        evidence_ref="profile-validation:old-predicate-after-reopen",
        predicate_id="runtime-profile-compatible",
        outcome="satisfied",
    )
    assert stale_assessment.get("obj-1").status == "active"
    with pytest.raises(ValueError, match="active objection"):
        DecisionEngine().resolve_with_events(record, "consent", "dispatch", store.events())

    fresh_assessment = assess_objection(
        ledger,
        objection_id="obj-1",
        actor="NAV",
        source="profile-validation:new-predicate",
        expected_revision=4,
        evidence_ref="profile-validation:new-runtime-current",
        predicate_id="new-runtime-profile-compatible",
        outcome="satisfied",
    )
    assert fresh_assessment.get("obj-1").status == "resolved"
    final = DecisionEngine().resolve_with_events(record, "consent", "dispatch", store.events())
    assert final.resolved_objection_ids == ("obj-1",)


def test_narrowing_preserves_initial_applicability_and_does_not_discharge(tmp_path):
    record = decision()
    store = EventStore(tmp_path / "events.jsonl")
    ledger = ObjectionLifecycle(store, record)
    raise_objection(ledger)
    narrowed = narrow_objection(
        ledger,
        objection_id="obj-1",
        actor="NAV",
        source="profile-review:selected-profile",
        expected_revision=1,
        scope="selected profile only",
        reason="limit this claim to the profile under consideration",
        evidence_refs=("candidate-profile:1",),
    )
    objection = narrowed.get("obj-1")
    assert objection.status == "narrowed"
    assert objection.applicability_revision == 2
    assert objection.applicability_history[0].scope == "runtime-profile compatibility"
    assert objection.applicability_history[1].scope == "selected profile only"
    assert objection.resolution_predicate_id == "runtime-profile-compatible"

    result = DecisionEngine().resolve_with_events(
        record, "recorded_disagreement", "proceed with the unresolved objection retained", store.events()
    )
    assert result.resolved_objection_ids == ()


def test_stale_transition_and_duplicate_or_out_of_order_events_fail_closed(tmp_path):
    record = decision()
    store = EventStore(tmp_path / "events.jsonl")
    ledger = ObjectionLifecycle(store, record)
    raise_objection(ledger)
    with pytest.raises(ValueError, match="stale or out-of-order"):
        assess_objection(
            ledger,
            objection_id="obj-1",
            actor="ENG",
            source="observation:stale",
            expected_revision=2,
            evidence_ref="evidence:1",
            predicate_id="unrelated",
            outcome="unknown",
        )
    event = store.events()[0]
    with pytest.raises(ValueError, match="conflicting duplicate"):
        materialize_objections([event, event.model_copy(update={"source": "changed-source"})], record)

    transition_before_raise = Event(
        event_type="objection_lifecycle",
        actor="ENG",
        source="observation:out-of-order",
        payload={
            "schema_version": 1,
            "operation": "assessed",
            "decision_id": "decision-1",
            "objection_id": "obj-1",
            "expected_revision": 1,
            "evidence_ref": "evidence:early",
            "predicate_id": "runtime-profile-compatible",
            "assessment": "satisfied",
        },
    )
    with pytest.raises(ValueError, match="precedes its raise"):
        materialize_objections([transition_before_raise, event], record)


def test_superseding_keeps_successor_active_and_duplicate_delivery_idempotent(tmp_path):
    record, store, ledger = supersession_case(tmp_path)
    state = ledger.supersede(
        "old",
        actor="NAV",
        source="decision:supersede",
        expected_revision=1,
        successor_id="new",
        reason="the new objection has the narrower, current claim",
    )
    assert state.get("old").status == "superseded"
    assert state.get("old").superseded_by == "new"
    assert [item.id for item in state.active_for_decision()] == ["new"]
    duplicate_delivery = materialize_objections([*store.events(), store.events()[0]], record)
    assert duplicate_delivery.objections == state.objections
    assert duplicate_delivery.metrics.events_scanned == state.metrics.events_scanned + 1
    assert duplicate_delivery.metrics.lifecycle_events == state.metrics.lifecycle_events
    assert duplicate_delivery.metrics.serialized_event_bytes > state.metrics.serialized_event_bytes
    assert duplicate_delivery.metrics.view_bytes == state.metrics.view_bytes


def test_supersession_rejects_self_reference_and_category_downgrade(tmp_path):
    record, store, ledger = supersession_case(tmp_path)
    ledger.supersede(
        "old",
        actor="NAV",
        source="decision:supersede",
        expected_revision=1,
        successor_id="new",
        reason="the new objection has the narrower, current claim",
    )
    self_supersede = Event(
        event_type="objection_lifecycle",
        actor="NAV",
        source="decision:self-supersede",
        payload={
            "operation": "superseded",
            "decision_id": record.id,
            "objection_id": "new",
            "expected_revision": 1,
            "successor_id": "new",
            "reason": "invalid self-reference",
        },
    )
    with pytest.raises(ValueError, match="cannot supersede itself"):
        materialize_objections([*store.events(), self_supersede], record)

    add_objection(
        ledger,
        objection_id="preference-successor",
        actor="ENG",
        source="review:preference-successor",
        category="preference",
        claim="I prefer a different profile",
        scope="profile choice",
        authority_scope="runtime",
        resolution_predicate_id="preference-noted",
    )
    with pytest.raises(ValueError, match="preserve objection category"):
        ledger.supersede(
            "new",
            actor="NAV",
            source="decision:downgrade",
            expected_revision=1,
            successor_id="preference-successor",
            reason="must not weaken a factual objection",
        )


def test_positions_are_immutable_and_initial_final_positions_stay_separate():
    engine = DecisionEngine()
    original = DecisionRecord(id="d", proposal="p", participants={"NAV"}, consequential=False)
    with pytest.raises(ValueError):
        DecisionPosition(participant="NAV", position=Position.BLOCK, rationale="untyped block")
    first = engine.capture_initial(original, DecisionPosition(participant="NAV", position=Position.SUPPORT))
    assert original.initial_positions == ()
    assert len(first.initial_positions) == 1
    with pytest.raises(ValidationError):
        first.initial_positions[0].position = Position.BLOCK
    with pytest.raises(ValidationError):
        first.initial_positions = ()
    final = engine.capture_final(
        first,
        DecisionPosition(participant="NAV", position=Position.STAND_ASIDE, category="resource", rationale="changed view"),
    )
    assert final.initial_positions[0].position is Position.SUPPORT
    assert final.final_positions[0].position is Position.STAND_ASIDE


def test_final_block_is_separate_and_still_requires_bounded_experiment_or_escalation():
    engine = DecisionEngine()
    record = DecisionRecord(
        id="d",
        proposal="p",
        participants={"NAV", "ENG"},
        authority_scope="routing",
        authority_scopes={"NAV": {"routing"}},
    )
    record = engine.capture_initial(record, DecisionPosition(participant="NAV", position=Position.SUPPORT))
    record = engine.capture_initial(record, DecisionPosition(participant="ENG", position=Position.SUPPORT))
    record = engine.capture_final(
        record,
        DecisionPosition(participant="ENG", position=Position.BLOCK, category="resource", rationale="capacity is unknown"),
    )
    with pytest.raises(ValueError, match="unresolved block"):
        engine.resolve(record, "consent", "dispatch")
    experimental = engine.resolve(record, "experiment", "run bounded capacity check")
    assert experimental.initial_positions[1].position is Position.SUPPORT
    assert experimental.final_positions[0].position is Position.BLOCK


def test_resource_objection_requires_explicit_resolution_and_safety_block_cannot_be_overridden():
    engine = DecisionEngine()
    resource_objection = DecisionRecord(
        id="resource-decision",
        proposal="select a runtime",
        participants={"ENG"},
        consequential=False,
        initial_positions=(
            DecisionPosition(
                participant="ENG",
                position=Position.OBJECT,
                category="resource",
                rationale="available memory is not yet measured",
            ),
        ),
    )
    with pytest.raises(ValueError, match="explicit resolution mode"):
        engine.resolve(resource_objection, "consent", "dispatch")
    assert engine.resolve(resource_objection, "recorded_disagreement", "dispatch with dissent").action == "dispatch with dissent"

    safety_block = DecisionRecord(
        id="safety-decision",
        proposal="dispatch runtime",
        participants={"ENG"},
        consequential=False,
        initial_positions=(
            DecisionPosition(
                participant="ENG",
                position=Position.BLOCK,
                category="safety",
                rationale="isolation boundary is unverified",
            ),
        ),
    )
    with pytest.raises(ValueError, match="unresolved block"):
        engine.resolve(safety_block, "recorded_disagreement", "dispatch")
    assert engine.resolve(safety_block, "experiment", "verify isolation").resolution == "experiment"


def test_preference_objection_is_visible_but_does_not_veto(tmp_path):
    record = DecisionRecord(
        id="preference-decision",
        proposal="select a runtime",
        participants={"NAV", "ENG"},
        consequential=False,
        authority_scopes={"NAV": {"runtime"}},
    )
    store = EventStore(tmp_path / "events.jsonl")
    ledger = ObjectionLifecycle(store, record)
    add_objection(
        ledger,
        objection_id="preference-1",
        actor="ENG",
        source="initial-position:ENG",
        category="preference",
        claim="I prefer the other profile",
        scope="profile choice",
        authority_scope="runtime",
        resolution_predicate_id="preference-recorded",
    )
    projection = ledger.projection
    assert [item.category for item in projection.active_for_decision()] == ["preference"]
    assert projection.blocking_for_decision() == ()
    resolved = DecisionEngine().resolve_with_events(record, "consent", "dispatch", store.events())
    assert resolved.action == "dispatch"


def test_initial_positions_and_resolution_rebuild_from_the_shared_event_store(tmp_path):
    store = EventStore(tmp_path / "events.jsonl")
    lifecycle = DecisionLifecycle.create(
        store,
        decision(with_initial_position=False),
        actor="NAV",
        source="decision:proposal",
    )
    with pytest.raises(ValueError, match="captured by its participant"):
        lifecycle.capture_initial(
            DecisionPosition(participant="ENG", position=Position.SUPPORT),
            actor="NAV",
            source="position:wrong-actor",
        )
    assert len(store.events()) == 1

    lifecycle.capture_initial(
        DecisionPosition(participant="NAV", position=Position.SUPPORT),
        actor="NAV",
        source="initial-position:NAV",
    )
    initial = DecisionPosition(
        participant="ENG",
        position=Position.OBJECT,
        category="factual",
        rationale="profile validation is stale",
        objection_id="obj-1",
        authority_scope="runtime",
        applicability_scope="runtime-profile compatibility",
        applicability_revision=1,
    )
    lifecycle.capture_initial(initial, actor="ENG", source="initial-position:ENG")
    ledger = ObjectionLifecycle(store, lifecycle.record)
    raise_objection(ledger)
    lifecycle = DecisionLifecycle(store, "decision-1")
    lifecycle.capture_final(
        DecisionPosition(participant="ENG", position=Position.SUPPORT, category="factual"),
        actor="ENG",
        source="final-position:ENG",
    )

    before = DecisionLifecycle(store, "decision-1").record
    assert before.initial_positions[1].position is Position.OBJECT
    assert before.final_positions[0].position is Position.SUPPORT
    with pytest.raises(ValueError, match="active objection"):
        lifecycle.resolve("consent", "dispatch", actor="NAV", source="resolution:early")
    assess_objection(
        ledger,
        objection_id="obj-1",
        actor="NAV",
        source="profile-validation:current",
        expected_revision=1,
        evidence_ref="profile-validation:current",
        predicate_id="runtime-profile-compatible",
        outcome="satisfied",
    )
    final = lifecycle.resolve("consent", "dispatch", actor="NAV", source="resolution:qualified")
    rebuilt = materialize_decision(EventStore(store.path).events(), "decision-1")
    assert final == rebuilt
    assert rebuilt.initial_positions[1].position is Position.OBJECT
    assert rebuilt.final_positions[0].position is Position.SUPPORT
    assert DecisionRecord.model_validate(rebuilt.model_dump()) == rebuilt
    duplicate_delivery = materialize_decision([*store.events(), store.events()[0]], "decision-1")
    assert duplicate_delivery == rebuilt
