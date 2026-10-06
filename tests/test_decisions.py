import pytest

from long_haul.decisions import DecisionEngine, DecisionRecord
from long_haul.models import DecisionPosition, Position


def test_scoped_authority_requires_actual_scope():
    record=DecisionRecord(id="d",proposal="p",participants={"NAV"},authority_scope="routing",authority_scopes={"NAV":{"routing"}},initial_positions=[DecisionPosition(participant="NAV",position=Position.CONSENT)])
    assert DecisionEngine().resolve(record,"scoped_authority","act","NAV").authority_holder == "NAV"
    bad=DecisionRecord(id="d",proposal="p",participants={"NAV"},authority_scope="routing",authority_scopes={"NAV":set()},initial_positions=[DecisionPosition(participant="NAV",position=Position.CONSENT)])
    with pytest.raises(ValueError): DecisionEngine().resolve(bad,"scoped_authority","act","NAV")
