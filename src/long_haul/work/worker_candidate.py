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

MAX_WORKER_FILES = 256
MAX_WORKER_DIRECTORIES = 256
MAX_WORKER_DEPTH = 64
MAX_WORKER_FILE_BYTES = 1048576


def worker_path(value: str) -> str:
    path = repository_path(value)
    if path == "." or any(part in {".pi", ".git"} for part in Path(path).parts):
        raise ValueError("worker target must be a regular repository file")
    return path


def _file_identity(info: os.stat_result):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _bounded_regular_read(path: Path, expected: os.stat_result) -> bytes:
    """Read one paused/stopped regular file without following a substituted link."""
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
             | getattr(os, "O_BINARY", 0))
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise ValueError("worker file could not be opened safely") from error
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or _file_identity(before) != _file_identity(expected) or before.st_size > MAX_WORKER_FILE_BYTES):
            raise ValueError("worker file changed during inspection")
        data = source.read(MAX_WORKER_FILE_BYTES + 1)
        after = os.fstat(source.fileno())
        if len(data) != before.st_size or _file_identity(after) != _file_identity(before):
            raise ValueError("worker file changed during bounded read")
        return data


def regular_contents(root: Path) -> dict[str, bytes]:
    """Only after process-tree cleanup, or while the executor has paused native execution."""
    result = {}
    directories = 0
    pending = [root]
    total = 0
    while pending:
        # Stream directory entries instead of materializing a potentially
        # unbounded directory listing before the count limit is applied.
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                # DirEntry.stat on Windows can report zero inode/link metadata.
                metadata = path.lstat()
                relative = worker_path(path.relative_to(root).as_posix())
                if len(Path(relative).parts) > MAX_WORKER_DEPTH:
                    raise ValueError("worker exceeds bounded directory depth")
                if stat.S_ISDIR(metadata.st_mode):
                    directories += 1
                    if directories > MAX_WORKER_DIRECTORIES:
                        raise ValueError("worker exceeds bounded directory count")
                    pending.append(path)
                    continue
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > MAX_WORKER_FILE_BYTES:
                    raise ValueError("worker contains a link, special file, or oversized file")
                if len(result) >= MAX_WORKER_FILES or total + metadata.st_size > 4194304:
                    raise ValueError("worker exceeds bounded candidate size")
                data = _bounded_regular_read(path, metadata)
                total += len(data)
                if total > 4194304 or len(data) > MAX_WORKER_FILE_BYTES:
                    raise ValueError("worker exceeds bounded candidate size")
                result[relative] = data
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
