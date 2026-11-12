"""Freeze exact Git content outside the worker's mutable workspace."""
from __future__ import annotations

import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .contracts import WorkContract
from .repository_evaluation_models import (
    CandidateFile,
    FrozenRepositoryCandidate,
    work_contract_fingerprint,
)
from .repository_git import _GitObjects, git_repository_identity
from .repository_models import content_digest, repository_path


@dataclass(frozen=True)
class CandidateSnapshot:
    candidate: FrozenRepositoryCandidate
    contents: Mapping[str, bytes]

    @contextmanager
    def materialize(self, *, scratch_root: Path | None = None):
        """Create a fresh executor-owned tree; no worker path is mounted."""
        with tempfile.TemporaryDirectory(prefix="long-haul-evaluator-", dir=scratch_root) as temporary:
            root = Path(temporary)
            for item in self.candidate.files:
                data = self.contents[item.path]
                if content_digest(data) != item.digest or len(data) != item.size_bytes:
                    raise ValueError("frozen candidate content changed")
                target = root / item.path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(0o555 if item.executable else 0o444)
            # Container uid 65534 can traverse this one temporary tree only.
            for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
                directory.chmod(0o555)
            root.chmod(0o555)
            try:
                yield root
            finally:
                # TemporaryDirectory needs writable directories for deterministic teardown.
                root.chmod(0o700)
                for directory in root.rglob("*"):
                    if directory.is_dir():
                        directory.chmod(0o700)


def freeze_repository_candidate(root: Path, commit: str, contract: WorkContract) -> CandidateSnapshot:
    extension = contract.repository_work
    if extension is None:
        raise ValueError("repository evaluation requires a repository work extension")
    actual_base = git_repository_identity(
        root, repository_id=extension.source.repository_id, commit=extension.source.commit,
        visibility=extension.source.visibility, data_class=extension.source.data_class,
        material_kinds={m.path: m.kind for m in extension.source.materials if m.kind != "submodule"},
    )
    if actual_base != extension.source:
        raise ValueError("repository base identity changed")
    source = git_repository_identity(root, repository_id=extension.source.repository_id, commit=commit,
                                     visibility=extension.source.visibility, data_class=extension.source.data_class)
    objects = _GitObjects(root)
    objects.run("merge-base", "--is-ancestor", extension.source.commit, source.commit)
    entries = objects.entries(source.commit)
    if len(entries) > 256:
        raise ValueError("candidate exceeds bounded file count")
    files, contents = [], {}
    for path, entry in sorted(entries.items()):
        repository_path(path)
        data = objects.blob(entry)  # Reject links/submodules; no checkout or filters.
        if sum(len(value) for value in contents.values()) + len(data) > 4194304:
            raise ValueError("candidate exceeds bounded byte count")
        contents[path] = data
        files.append(CandidateFile(path=path, digest=content_digest(data), size_bytes=len(data),
                                   executable=entry[0] == "100755"))
    changed = objects.run("diff-tree", "--no-commit-id", "--no-renames", "--name-only", "-r", "-z",
                          extension.source.commit, source.commit).split(b"\x00")
    changed_paths = tuple(repository_path(path.decode()) for path in changed if path)
    candidate = FrozenRepositoryCandidate(base=extension.source, source=source, files=tuple(files),
                                          changed_paths=changed_paths,
                                          contract_fingerprint=work_contract_fingerprint(contract))
    return CandidateSnapshot(candidate=candidate, contents=MappingProxyType(contents))
