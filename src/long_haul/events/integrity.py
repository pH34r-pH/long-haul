"""Streaming JSONL replay diagnostics, hash-chain verification, and cross-platform locking (#288, #289)."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SUPPORTED_SCHEMA_VERSION = 1

IntegrityStatus = Literal[
    "verified_against_anchor",
    "locally_consistent_unanchored",
    "legacy_unverified",
    "corrupted",
]

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[Path, threading.RLock] = {}


@dataclass(frozen=True)
class ReplayDiagnostic:
    """Typed replay, schema, or integrity diagnostic with exact location."""

    path: str
    line: int
    offset: int
    reason: str
    event_id: str | None = None
    raw_type: str | None = None
    schema_version: int | None = None


class ReplayError(ValueError):
    """Typed replay, projection, lock, or chain-integrity failure."""

    def __init__(self, diagnostic: ReplayDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(
            f"{diagnostic.path}:{diagnostic.line} (offset {diagnostic.offset}): {diagnostic.reason}"
        )


@dataclass(frozen=True)
class QuarantinedRecord:
    """Unknown event type or unsupported schema version preserved without authority."""

    line: int
    offset: int
    raw_line: str
    parsed: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class ChainCheckpoint:
    """Trusted anchor receipt for an event log head."""

    log_id: str
    seq: int
    head_hash: str
    event_count: int


@dataclass
class ReplayResult:
    """Result of streaming replay and integrity inspection."""

    events: list[Any] = field(default_factory=list)
    quarantined: list[QuarantinedRecord] = field(default_factory=list)
    incomplete_tail: ReplayDiagnostic | None = None
    diagnostics: list[ReplayDiagnostic] = field(default_factory=list)
    integrity_status: IntegrityStatus = "legacy_unverified"
    checkpoint: ChainCheckpoint | None = None

    @property
    def complete(self) -> bool:
        return (
            not self.quarantined
            and self.incomplete_tail is None
            and not self.diagnostics
            and self.integrity_status != "corrupted"
        )


def canonical_event_json(mapping: dict[str, Any]) -> str:
    """Deterministic JSON encoding excluding ``event_hash`` for hash chaining."""
    core = {k: v for k, v in mapping.items() if k != "event_hash"}
    return json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def genesis_hash(log_id: str) -> str:
    return hashlib.sha256(f"long-haul-event-genesis-v1:{log_id}".encode()).hexdigest()


def compute_event_hash(mapping: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_event_json(mapping).encode("utf-8")).hexdigest()


def _thread_lock_for(path: Path) -> threading.RLock:
    resolved = path.resolve()
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[resolved] = lock
        return lock


def _try_os_lock(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_os_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusive_file_lock(target: Path, *, timeout_s: float = 5.0) -> Iterator[None]:
    """Serialize local threads and processes around an event-store critical section.

    Valid for local POSIX and Windows filesystems; network filesystems (NFS/SMB)
    are not qualified for advisory locking durability guarantees.
    """
    lock_path = target.with_suffix(target.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    tlock = _thread_lock_for(lock_path)
    deadline = time.monotonic() + max(0.0, timeout_s)
    if not tlock.acquire(timeout=max(0.0, timeout_s)):
        raise ReplayError(ReplayDiagnostic(str(target), 0, 0, f"lock timeout after {timeout_s}s"))
    fd: int | None = None
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        while not _try_os_lock(fd):
            if time.monotonic() >= deadline:
                raise ReplayError(ReplayDiagnostic(str(target), 0, 0, f"lock timeout after {timeout_s}s"))
            time.sleep(0.01)
        yield
    finally:
        if fd is not None:
            _release_os_lock(fd)
            os.close(fd)
        tlock.release()


def iter_raw_jsonl_lines(path: Path) -> Iterator[tuple[int, int, str, bool, bool]]:
    """Yield ``(line_no, byte_offset, text, ended_with_newline, is_last)`` line-by-line."""
    if not path.exists():
        return
    file_size = path.stat().st_size
    with path.open("rb") as handle:
        line_no = 0
        while True:
            offset = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            line_no += 1
            is_last = handle.tell() >= file_size
            ended_with_newline = raw.endswith(b"\n")
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            yield line_no, offset, text, ended_with_newline, is_last


def _verify_anchor(
    path: Path,
    checkpoint: ChainCheckpoint,
    seen_hashes: dict[int, str],
    record_count: int,
    anchor: ChainCheckpoint,
) -> IntegrityStatus:
    if anchor.log_id != checkpoint.log_id:
        raise ReplayError(
            ReplayDiagnostic(str(path), 1, 0, f"anchor log_id mismatch: expected {anchor.log_id}, got {checkpoint.log_id}")
        )
    if checkpoint.seq < anchor.seq:
        raise ReplayError(
            ReplayDiagnostic(str(path), record_count, 0, f"log truncated/rolled back: head seq {checkpoint.seq} < anchor seq {anchor.seq}")
        )
    if seen_hashes.get(anchor.seq) != anchor.head_hash:
        raise ReplayError(
            ReplayDiagnostic(str(path), anchor.seq, 0, f"anchor hash mismatch at seq {anchor.seq}: history rewritten")
        )
    return "verified_against_anchor"


def _verify_one_chained_record(
    path: Path,
    line_no: int,
    offset: int,
    rec: dict[str, Any],
    *,
    expected_log_id: str | None,
    expected_seq: int,
    expected_prev: str | None,
) -> tuple[str, str]:
    event_id = str(rec.get("id")) if rec.get("id") is not None else None
    rec_log_id = rec.get("log_id")
    if not isinstance(rec_log_id, str) or not rec_log_id:
        raise ReplayError(ReplayDiagnostic(str(path), line_no, offset, "missing log_id in chained record", event_id))
    log_id = rec_log_id if expected_log_id is None else expected_log_id
    prev_expected = genesis_hash(log_id) if expected_log_id is None else expected_prev
    if rec_log_id != log_id:
        raise ReplayError(ReplayDiagnostic(str(path), line_no, offset, f"log_id mismatch: expected {log_id}, got {rec_log_id}", event_id))
    if rec.get("seq") != expected_seq:
        raise ReplayError(ReplayDiagnostic(str(path), line_no, offset, f"sequence mismatch: expected {expected_seq}, got {rec.get('seq')}", event_id))
    if rec.get("prev_hash") != prev_expected:
        raise ReplayError(
            ReplayDiagnostic(str(path), line_no, offset, f"prev_hash mismatch at seq {expected_seq}: expected {prev_expected}, got {rec.get('prev_hash')}", event_id)
        )
    actual_hash = compute_event_hash(rec)
    if rec.get("event_hash") != actual_hash:
        raise ReplayError(
            ReplayDiagnostic(str(path), line_no, offset, f"event_hash mismatch at seq {expected_seq}: expected {actual_hash}, got {rec.get('event_hash')}", event_id)
        )
    return log_id, actual_hash


def verify_chain_records(
    path: Path,
    records: list[tuple[int, int, dict[str, Any]]],
    *,
    anchor: ChainCheckpoint | None = None,
) -> tuple[IntegrityStatus, ChainCheckpoint | None]:
    """Verify monotonic sequence, genesis binding, and SHA-256 hash links."""
    if not records:
        if anchor is not None and anchor.seq > 0:
            raise ReplayError(ReplayDiagnostic(str(path), 0, 0, "log is empty but trusted anchor requires records"))
        return ("verified_against_anchor" if anchor is not None else "legacy_unverified"), anchor

    has_chain = any(
        rec.get("event_hash") is not None or rec.get("seq") is not None or rec.get("prev_hash") is not None
        for _, _, rec in records
    )
    if not has_chain:
        if anchor is not None:
            raise ReplayError(ReplayDiagnostic(str(path), 1, 0, "legacy unchained log cannot satisfy trusted anchor"))
        return "legacy_unverified", None

    log_id: str | None = None
    expected_prev: str | None = None
    last_hash: str | None = None
    seen_hashes: dict[int, str] = {}
    for seq, (line_no, offset, rec) in enumerate(records, start=1):
        log_id, last_hash = _verify_one_chained_record(
            path, line_no, offset, rec, expected_log_id=log_id, expected_seq=seq, expected_prev=expected_prev,
        )
        seen_hashes[seq] = last_hash
        expected_prev = last_hash

    assert log_id is not None and last_hash is not None
    checkpoint = ChainCheckpoint(log_id=log_id, seq=len(records), head_hash=last_hash, event_count=len(records))
    if anchor is not None:
        return _verify_anchor(path, checkpoint, seen_hashes, len(records), anchor), checkpoint
    return "locally_consistent_unanchored", checkpoint
