#!/usr/bin/env python3
"""Check locally supplied DPV release bytes against the reviewed source pin."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "contracts/resource-policy/dpv-source-pin.json"
UPSTREAM_PATH = "2.0/dpv/dpv.ttl"
NAMESPACE = "https://w3id.org/dpv#"
MAX_SOURCE_BYTES = 2 * 1024 * 1024


def verify_source(source: Path, manifest: dict) -> dict:
    if not isinstance(manifest, dict):
        raise TypeError("invalid DPV source pin")
    revision, digest = manifest.get("commit"), manifest.get("sha256")
    if (not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or manifest.get("upstream_path") != UPSTREAM_PATH or manifest.get("namespace") != NAMESPACE):
        raise ValueError("invalid DPV source pin")
    with source.open("rb") as stream:
        data = stream.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("DPV source exceeds byte limit")
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("DPV source hash mismatch")
    return {"commit": revision, "upstream_path": UPSTREAM_PATH, "sha256": digest, "bytes": len(data)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="locally downloaded upstream Turtle file")
    args = parser.parse_args()
    try:
        result = verify_source(args.source, json.loads(MANIFEST.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f"DPV source verification failed: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
