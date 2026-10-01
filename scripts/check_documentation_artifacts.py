#!/usr/bin/env python3
"""Bounded checks for changed living Markdown and incidental artifacts."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

LIVING_DOC_EXCEPTIONS = {"README.md", "AGENTS.md", "CONTRIBUTING.md", "SECURITY.md", "LICENSING.md"}
LIVING_ROOTS = ("docs/", "infra/")
HISTORICAL_ROOTS = (
    "artifacts/",
    "evidence/",
    "qualification/",
    "research/",
    "results/",
    "docs/archive/",
    "docs/history/",
)
FORBIDDEN_PARTS = {
    ".cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "cache",
    "dist",
    "node_modules",
    "temp",
    "tmp",
}
FORBIDDEN_SUFFIXES = {".bak", ".pyc", ".pyo", ".swp", ".tmp"}
INCIDENTAL_DOC_NAMES = {"cache.md", "scratch.md", "temp.md", "tmp.md", "untitled.md"}
DESCRIPTIVE_SLUG = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
ISSUE_ONLY = re.compile(r"^(?:issue[-_]?)?\d+$", re.IGNORECASE)


def _relative(path: str | Path, root: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate
    return candidate.resolve().relative_to(root.resolve())


def is_historical_doc(path: str | Path) -> bool:
    return any(Path(path).as_posix().startswith(root) for root in HISTORICAL_ROOTS)


def is_living_doc(path: Path) -> bool:
    if path.suffix.lower() not in {".md", ".markdown"} or is_historical_doc(path):
        return False
    normalized = path.as_posix()
    return path.name in LIVING_DOC_EXCEPTIONS or any(normalized.startswith(root) for root in LIVING_ROOTS)


def _check_doc_name(path: Path, new_path: bool) -> list[str]:
    if not new_path or not is_living_doc(path) or path.name in LIVING_DOC_EXCEPTIONS:
        return []
    if path.name.lower() in INCIDENTAL_DOC_NAMES or ISSUE_ONLY.fullmatch(path.stem):
        return [f"{path}: new living Markdown needs a descriptive name"]
    if not DESCRIPTIVE_SLUG.fullmatch(path.stem):
        return [f"{path}: new living Markdown needs a lowercase descriptive name"]
    return []


def _check_artifact(path: Path, new_path: bool) -> list[str]:
    if not new_path:
        return []
    parts = {part.lower() for part in path.parts}
    if parts & FORBIDDEN_PARTS or path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return [f"{path}: new or changed path is an unapproved temporary/build artifact"]
    if path.name in {".DS_Store", "Thumbs.db"}:
        return [f"{path}: new or changed path is an unapproved temporary/build artifact"]
    return []


def changed_files(root: Path, base: str) -> list[tuple[str, str]]:
    output = subprocess.check_output(
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--find-copies",
            "--diff-filter=ACMR",
            f"{base}...HEAD",
        ],
        cwd=root,
        text=False,
    )
    fields = output.decode().split("\0")
    records: list[tuple[str, str]] = []
    index = 0
    while index < len(fields) - 1:
        status = fields[index]
        index += 1
        if status.startswith(("R", "C")):
            index += 1  # old name; check the destination below
            records.append((status[0], fields[index]))
        else:
            records.append((status[0], fields[index]))
        index += 1
    return records


def changed_living_markdown(root: Path, base: str) -> list[str]:
    return [path for _, path in changed_files(root, base) if is_living_doc(Path(path))]


def check_paths(root: Path, records: list[tuple[str, str]]) -> list[str]:
    errors: list[str] = []
    for status, raw_path in records:
        path = _relative(raw_path, root)
        if "\n" in raw_path or "\t" in raw_path:
            errors.append(f"{raw_path!r}: path contains a control character")
            continue
        new_path = status in {"A", "C", "R"}
        errors.extend(_check_artifact(path, new_path))
        errors.extend(_check_doc_name(path, new_path))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="comparison base revision")
    parser.add_argument("--print-living-markdown", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    records = changed_files(root, args.base)
    if args.print_living_markdown:
        print("\n".join(changed_living_markdown(root, args.base)))
        return 0
    errors = check_paths(root, records)
    if errors:
        print("Documentation/artifact guard failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print(f"Documentation/artifact guard passed for {len(records)} changed path(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
