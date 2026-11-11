"""Build a Git candidate from stopped worker bytes without executing its code."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from .contracts import WorkContract
from .repository_candidate import CandidateSnapshot, freeze_repository_candidate
from .repository_git import _GitObjects
from .repository_models import content_digest, repository_path


def worker_path(value: str) -> str:
    path = repository_path(value)
    if path == "." or any(part in {".pi", ".git"} for part in Path(path).parts):
        raise ValueError("worker target must be a regular repository file")
    return path


def regular_contents(root: Path) -> dict[str, bytes]:
    """Only after process-tree cleanup, or while the executor has paused native execution."""
    result = {}
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        relative = worker_path(path.relative_to(root).as_posix())
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode) or path.stat().st_nlink != 1 or path.stat().st_size > 1048576:
            raise ValueError("worker contains a link, special file, or oversized file")
        result[relative] = path.read_bytes()
        if len(result) > 256 or sum(map(len, result.values())) > 4194304:
            raise ValueError("worker exceeds bounded candidate size")
    return result


def _git(root, *arguments, data=None):
    environment = {"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
                   "GIT_AUTHOR_NAME": "Long Haul worker freeze", "GIT_AUTHOR_EMAIL": "worker@example.invalid",
                   "GIT_COMMITTER_NAME": "Long Haul worker freeze", "GIT_COMMITTER_EMAIL": "worker@example.invalid",
                   "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}
    result = subprocess.run([shutil.which("git", path=os.defpath), "-C", str(root), "-c", "core.hooksPath=/dev/null", *arguments],
                            input=data, env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=10, check=True)
    if len(result.stdout) > 1048576:
        raise ValueError("candidate Git metadata exceeds bound")
    return result.stdout.strip()


def _tree(root, files):
    entries = []
    directories = {}
    for path, (data, executable) in sorted(files.items()):
        first, separator, rest = path.partition("/")
        if separator:
            directories.setdefault(first, {})[rest] = (data, executable)
        else:
            oid = _git(root, "hash-object", "-w", "--stdin", data=data)
            entries.append((first, b"100755" if executable else b"100644", b"blob", oid))
    for name, children in directories.items():
        entries.append((name, b"040000", b"tree", _tree(root, children)))
    payload = b"".join(mode + b" " + kind + b" " + oid + b"\t" + name.encode() + b"\0"
                       for name, mode, kind, oid in sorted(entries))
    return _git(root, "mktree", "-z", data=payload)


def freeze_stopped_worker(source_root: Path, base: CandidateSnapshot, worker: Path,
                          projected_paths: set[str], contract: WorkContract) -> CandidateSnapshot:
    """Caller must supply the exact successful container-removal receipt first."""
    contents = regular_contents(worker)
    if not set(contents) <= projected_paths:
        raise ValueError("worker introduced an unadmitted target")
    files = {item.path: (base.contents[item.path], item.executable) for item in base.candidate.files}
    for path in projected_paths:
        if path in contents:
            previous = files.get(path, (b"", False))
            files[path] = (contents[path], previous[1])
        elif path in files:
            del files[path]
    changed = {path for path in set(files) | set(base.contents)
               if path not in files or path not in base.contents or content_digest(files[path][0]) != content_digest(base.contents[path])}
    if any(not contract.permits_path(path) for path in changed):
        raise ValueError("worker candidate changes escaped contract scope")
    # Existing bounded evaluator freeze rechecks the exact base, all objects, file
    # count, byte limits and ancestry. Git sees only trusted arguments and stdin.
    source_objects = _GitObjects(source_root).run("rev-parse", "--path-format=absolute", "--git-path", "objects").decode()
    with tempfile.TemporaryDirectory(prefix="long-haul-worker-git-") as temporary:
        root = Path(temporary)
        _git(root, "init", "--bare", "-q", "--object-format=" + ("sha256" if len(base.candidate.base.commit) == 64 else "sha1"))
        (root / "objects/info/alternates").write_text(source_objects + "\n")
        tree = _tree(root, files)
        commit = _git(root, "commit-tree", tree.decode(), "-p", base.candidate.base.commit,
                      data=b"Freeze stopped worker candidate\n").decode()
        return freeze_repository_candidate(root, commit, contract)
