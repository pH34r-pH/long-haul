"""Regression checks for the measured Anchorage 7B profile handoff."""

import json
from pathlib import Path


PROFILE = Path(__file__).parents[1] / "contracts" / "anchorage-7b-profile.json"


def test_anchorage_profile_preserves_unqualified_authorization() -> None:
    document = json.loads(PROFILE.read_text())
    assert document["qualification"] == {
        "hardware_qualified": False,
        "authorization": "operator-risk-accepted-unqualified",
        "evidence_scope": "bounded measured benchmark; not hardware qualification",
    }
    assert document["runtime"]["source_commit"] == "7fe450e19305b828c199d602c23a8337aaa1f03b"
    assert document["runtime"]["cuda_architectures"] == "61"


def test_anchorage_profiles_are_distinct_and_measured() -> None:
    document = json.loads(PROFILE.read_text())
    profiles = document["profiles"]
    ids = {profile["id"] for profile in profiles}
    assert len(ids) == len(profiles) == 8
    assert all(profile["validation"]["state"] == "SUPPORTED" for profile in profiles)
    assert all(profile["validation"]["provenance"] == "measured" for profile in profiles)
    assert any(profile["resources"] == ["ANC-G0"] for profile in profiles)
    assert any(profile["resources"] == ["ANC-G1"] for profile in profiles)
    assert any(profile["resources"] == ["ANC-G0", "ANC-G1"] for profile in profiles)
    assert all(
        not (set(profile["resources"]) == {"ANC-G0", "ANC-G1"} and profile["options"].get("tensor_split") == "1.00")
        for profile in profiles
    )
