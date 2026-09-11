"""Manifest loading is the public entry point for all current and future crew."""
from pathlib import Path

import yaml

from .models import CrewMember, Vessel


def load_crew(path: str | Path) -> CrewMember:
    return CrewMember.model_validate(yaml.safe_load(Path(path).read_text()))

def load_vessel(path: str | Path) -> Vessel:
    return Vessel.model_validate(yaml.safe_load(Path(path).read_text()))
