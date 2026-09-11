from long_haul.models import (
    CrewMember,
    Decision,
    DecisionPosition,
    Position,
    Resource,
    Vessel,
)


def test_crew_identity_is_distinct_from_embodiment():
    crew = CrewMember(
        crew_id="NAV-01",
        callsign="Mako",
        primary_role="navigator",
        embodiment=None,
    )
    assert crew.crew_id == "NAV-01"
    assert crew.embodiment is None


def test_vessel_preserves_distinct_resources():
    vessel = Vessel(
        id="anchorage",
        name="Anchorage",
        class_name="station",
        resources=[
            Resource(id="ANC-G0", kind="gpu", model="GTX 1070", memory_mb=8192),
            Resource(id="ANC-G1", kind="gpu", model="GTX 1070", memory_mb=8192),
        ],
    )
    assert vessel.resources[0].id != vessel.resources[1].id


def test_dissent_survives_decision_outcome():
    decision = Decision(
        decision_id="D-1",
        proposal="Run on Kestrel",
        positions=[
            DecisionPosition(participant="NAV-01", position=Position.CONSENT),
            DecisionPosition(
                participant="ENG-01",
                position=Position.STAND_ASIDE,
                category="resource",
                rationale="Anchorage may be faster, but Kestrel is feasible.",
            ),
        ],
        outcome="consent",
        action="Run on Kestrel",
    )
    assert decision.positions[1].position is Position.STAND_ASIDE
    assert decision.action == "Run on Kestrel"
