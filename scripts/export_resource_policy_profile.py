#!/usr/bin/env python3
"""Export the authoring JSON Schema and verify profile artifacts.

This script intentionally requires only the pinned Long Haul environment. It does
not perform network resolution or require an RDF store.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from long_haul.resource_policy import (
    JSONLD_CONTEXT,
    RESOURCE_POLICY_PROFILE_ID,
    profile_json_schema,
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def export(output_dir: Path) -> None:
    schema = profile_json_schema()
    schema["$id"] = (
        RESOURCE_POLICY_PROFILE_ID + "/resource-policy.schema.json"
    )
    schema["title"] = "Long-Haul Resource Policy Authoring Projection v1"
    write_json(output_dir / "resource-policy.schema.json", schema)
    write_json(output_dir / "context.generated.jsonld", {"@context": JSONLD_CONTEXT})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("build/resource-policy"),
    )
    args = parser.parse_args()
    export(args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
