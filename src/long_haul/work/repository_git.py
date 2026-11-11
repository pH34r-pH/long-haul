"""Frozen Git object reads; never follow workspace links, filters, or submodules."""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from .repository_models import (
    ContextProviderIdentity,
    DigestReference,
    RepositoryDocument,
    RepositoryIdentity,
    RepositoryMaterial,
    content_digest,
    repository_path,
)

MAX_BLOB_BYTES = 1048576


class _GitObjects:
    def __init__(self, root: Path):
        self.root = root.resolve(strict=True)
        self.git = shutil.which("git", path=os.defpath)
        if self.git is None:
            raise RuntimeError("Git context provider unavailable")

    def run(self, *arguments: str) -> bytes:
        environment = {
            "PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
        }
        result = subprocess.run(
            [self.git, "--no-pager", "-C", str(self.root), "-c", "core.fsmonitor=false", *arguments],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True,
        )
        if len(result.stdout) > MAX_BLOB_BYTES:
            raise ValueError("Git metadata exceeds bounded provider limit")
        return result.stdout

    def entries(self, commit: str) -> dict[str, tuple[str, str]]:
        entries = {}
        for line in self.run("ls-tree", "-r", "-z", commit).split(b"\x00"):
            if not line:
                continue
            metadata, path = line.split(b"\t", 1)
            mode, _, object_id = metadata.decode("ascii").split()
            entries[path.decode("utf-8")] = (mode, object_id)
        return entries

    def blob(self, entry: tuple[str, str]) -> bytes:
        mode, object_id = entry
        if mode not in {"100644", "100755"}:
            raise PermissionError("links, submodules and non-file objects are not context files")
        size = int(self.run("cat-file", "-s", object_id))
        if size > MAX_BLOB_BYTES:
            raise ValueError("source exceeds bounded provider limit")
        return self.run("cat-file", "blob", object_id)


def _material_identity(kind: str, content: bytes) -> str:
    if kind == "generated":
        return content_digest(content)
    if kind == "lfs":
        match = re.search(rb"^oid (sha256:[0-9a-f]{64})$", content, re.MULTILINE)
        if content.startswith(b"version https://git-lfs.github.com/spec/v1\n") and match:
            return match.group(1).decode("ascii")
    raise ValueError("invalid or unavailable material identity")


def git_repository_identity(
    root: Path, *, repository_id: str, commit: str, visibility: str = "public",
    data_class: str = "public", material_kinds: dict[str, str] | None = None,
) -> RepositoryIdentity:
    # Validate object IDs before passing even a revision expression to Git.
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("an exact Git commit object ID is required")
    objects = _GitObjects(root)
    if objects.run("cat-file", "-t", commit).strip() != b"commit":
        raise ValueError("source identity must name a Git commit object")
    tree = objects.run("rev-parse", "--verify", commit + "^{tree}").decode().strip()
    entries = objects.entries(commit)
    materials = [RepositoryMaterial(path=path, kind="submodule", identity=oid)
                 for path, (mode, oid) in entries.items() if mode == "160000"]
    for path, kind in (material_kinds or {}).items():
        repository_path(path)
        materials.append(RepositoryMaterial(
            path=path, kind=kind, identity=_material_identity(kind, objects.blob(entries[path])),
        ))
    return RepositoryIdentity(repository_id=repository_id, commit=commit, tree=tree,
                              visibility=visibility, data_class=data_class, materials=tuple(materials))


class GitContextProvider:
    """Read admitted files from an exact commit, independently of checkout state."""

    def __init__(self, root: Path, source: RepositoryIdentity):
        actual = git_repository_identity(
            root, repository_id=source.repository_id, commit=source.commit,
            visibility=source.visibility, data_class=source.data_class,
            material_kinds={m.path: m.kind for m in source.materials if m.kind != "submodule"},
        )
        if actual != source:
            raise ValueError("stale or mismatched Git tree/material identity")
        self.source = source
        self._objects = _GitObjects(root)
        self._entries = self._objects.entries(source.commit)
        modules = (Path(__file__), Path(__file__).with_name("repository_context.py"))
        implementation = content_digest(b"".join(path.read_bytes() for path in modules))
        self.identity = ContextProviderIdentity(
            name="git-python-ast", version="1", tool_versions=(
                self._objects.run("--version").decode().strip(), "Python " + platform.python_version(),
            ), implementation=DigestReference(reference="urn:long-haul:context:git-python-ast-v1", digest=implementation),
            configuration=(("max_blob_bytes", str(MAX_BLOB_BYTES)), ("network", "none"),
                           ("textconv", "disabled"), ("replace_objects", "disabled")),
        )

    def read(self, path: str) -> RepositoryDocument:
        repository_path(path)
        if path not in self._entries:
            raise FileNotFoundError(path)
        entry = self._entries[path]
        try:
            content = self._objects.blob(entry)
        except PermissionError:
            raise
        except (subprocess.SubprocessError, OSError) as exc:
            raise NotImplementedError("frozen Git object unavailable; no acquisition permitted") from exc
        if (content.startswith(b"version https://git-lfs.github.com/spec/v1\n")
                and not any(m.path == path and m.kind == "lfs" for m in self.source.materials)):
            raise ValueError("LFS source identity was not frozen; no download attempted")
        return RepositoryDocument(
            reference=DigestReference(reference=f"git:{self.source.commit}:{entry[1]}", digest=content_digest(content)),
            text=content.decode("utf-8"),
        )
