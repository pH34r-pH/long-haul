#!/usr/bin/env python3
"""Verify locally supplied upstream ODRL AI draft bytes against a reviewed pin."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "contracts/resource-policy/odrl-ai-source-pin.json"
ACTIONS = ("ai-training", "ai-train-gen", "ai-augment", "data-mining", "search", "index")
FILES = {"index.html": "ai-vocab/index.html", "ai-vocab.ttl": "ai-vocab/ai-vocab.ttl"}


def _source_files(manifest: dict, source_dir: Path) -> tuple[str, dict, dict]:
    if not isinstance(manifest, dict):
        raise TypeError("invalid ODRL AI source pin")
    revision = manifest.get("commit", "")
    files = manifest.get("files", {})
    if (not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)
            or not isinstance(files, dict) or set(files) != set(FILES)):
        raise ValueError("invalid ODRL AI source pin")
    content = {}
    for name, upstream in FILES.items():
        entry = files[name]
        if (not isinstance(entry, dict) or entry.get("upstream_path") != upstream
                or not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise ValueError("invalid upstream source entry")
        data = (source_dir / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError(f"source hash mismatch: {name}")
        content[name] = data.decode("utf-8")
    return revision, files, content


def _verify_actions(html: str) -> None:
    sections = {}
    for action in ACTIONS:
        section = re.search(r'<section id="term-' + re.escape(action) + r'">(.*?)</section>',
                            html, re.DOTALL)
        if section is None or "http://www.w3.org/ns/odrl/2/" + action not in section.group(1):
            raise ValueError(f"reviewed action missing: {action}")
        sections[action] = section.group(1)
    if not re.search(r'Included In:</th>\s*<td>\s*<a href="#?term-ai-training"', sections["ai-train-gen"]):
        raise ValueError("reviewed generative-training hierarchy missing")


def verify_sources(manifest: dict, source_dir: Path) -> dict:
    revision, files, content = _source_files(manifest, source_dir)
    _verify_actions(content["index.html"])
    ttl = content["ai-vocab.ttl"]
    statements = [line.strip() for line in ttl.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if "TODO" not in ttl or any(not re.fullmatch(r"PREFIX\s+[A-Za-z][\w-]*:\s*<[^>]+>", line, re.IGNORECASE)
                                for line in statements):
        raise ValueError("Turtle source changed; review its machine-readable vocabulary")
    return {"commit": revision, "files": {name: files[name]["sha256"] for name in FILES},
            "actions_checked": list(ACTIONS), "generative_included_in_training": True,
            "turtle_status": "todo-skeleton"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True, help="directory containing the two upstream files")
    args = parser.parse_args()
    try:
        result = verify_sources(json.loads(MANIFEST.read_text(encoding="utf-8")), args.source_dir)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f"ODRL AI source verification failed: {error}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
