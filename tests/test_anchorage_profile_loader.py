import json
from pathlib import Path

import pytest

from long_haul.benchmark_scheduler import load_profile_contract


def test_load_published_anchorage_contract():
    path = Path(__file__).parents[1] / "contracts" / "anchorage-7b-profile.json"
    profiles, validations = load_profile_contract(path)

    assert len(profiles) == 8
    assert len(validations) == 8
    assert {profile.participating_resources[0] for profile in profiles[:3]} == {"ANC-C0", "ANC-G0", "ANC-G1"}
    assert all(validation.provenance == "measured" for validation in validations)
    assert all(validation.depth.value == "BENCHMARK" for validation in validations)


def test_profile_contract_fails_closed_on_qualification(tmp_path):
    source = json.loads(
        (Path(__file__).parents[1] / "contracts" / "anchorage-7b-profile.json").read_text()
    )
    source["qualification"]["hardware_qualified"] = True
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(source))

    with pytest.raises(ValueError, match="hardware_qualified"):
        load_profile_contract(path)
