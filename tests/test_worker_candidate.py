"""Stopped byte snapshots reuse the exact existing Git/evaluator candidate model."""
import os
from pathlib import Path

import pytest
from repository_evaluator_fixture import PROGRAM, git, setup
from test_harness_trajectory import reference

from long_haul.work.repository_candidate import freeze_repository_candidate
from long_haul.work.worker_candidate import (
    _bounded_regular_read,
    _git,
    freeze_stopped_worker,
    regular_contents,
)


def test_worker_freeze_preserves_unprojected_base_and_exact_git_identity(tmp_path):
    root, work, _, _ = setup(tmp_path, reference("sandbox"))
    base = freeze_repository_candidate(root, work.repository_work.source.commit, work)
    worker = tmp_path / "worker"
    worker.mkdir()
    (worker / "program.py").write_bytes(PROGRAM)
    snapshot = freeze_stopped_worker(root, base, worker, {"program.py"}, work)
    assert snapshot.candidate.changed_paths == ("program.py",)
    assert snapshot.contents["README.md"] == base.contents["README.md"]
    assert snapshot.contents["program.py"] == PROGRAM
    assert snapshot.candidate.base == base.candidate.base
    assert git(root, "rev-parse", "HEAD") != snapshot.candidate.source.commit
    repeated = freeze_stopped_worker(root, base, worker, {"program.py"}, work)
    assert repeated.candidate == snapshot.candidate


def test_worker_freeze_cannot_add_unadmitted_path(tmp_path):
    root, work, base, _ = setup(tmp_path, reference("sandbox"))
    worker = tmp_path / "worker"
    worker.mkdir()
    (worker / "private").write_text("unadmitted")
    with pytest.raises(ValueError, match="unadmitted"):
        freeze_stopped_worker(root, base, worker, {"program.py"}, work)


def test_trusted_git_resolution_ignores_caller_path_shim(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    shim = tmp_path / "bin"
    shim.mkdir()
    marker = tmp_path / "shim-executed"
    (shim / "git").write_text("#!/bin/sh\ntouch '" + str(marker) + "'\nexit 1\n")
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(shim))
    assert _git(root, "--version").startswith(b"git version")
    from long_haul.work.repository_git import _GitObjects
    assert Path(_GitObjects(root).git).parent != shim
    assert not marker.exists()


def test_worker_scan_preserves_binary_bytes(tmp_path):
    worker = tmp_path / "worker"
    worker.mkdir()
    (worker / "file.bin").write_bytes(b"\r\n\x1a\x00")
    assert regular_contents(worker) == {"file.bin": b"\r\n\x1a\x00"}


def test_worker_scan_rejects_file_replaced_after_inspection(tmp_path, monkeypatch):
    worker = tmp_path / "worker"
    worker.mkdir()
    target = worker / "file.txt"
    target.write_bytes(b"allowed")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"changed")
    original_open = os.open

    def swap_before_open(path, flags, *args, **kwargs):
        if Path(path) == target:
            os.replace(replacement, target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(ValueError, match="changed during inspection"):
        regular_contents(worker)


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks requires Windows developer privileges")
def test_worker_scan_rejects_link_substituted_after_inspection(tmp_path, monkeypatch):
    worker = tmp_path / "worker"
    worker.mkdir()
    target = worker / "file.txt"
    target.write_bytes(b"allowed")
    outside = tmp_path / "private.txt"
    outside.write_bytes(b"private")
    original_open = os.open

    def replace_with_link(path, flags, *args, **kwargs):
        if Path(path) == target:
            target.unlink()
            target.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_with_link)
    with pytest.raises(ValueError, match="opened safely|changed during inspection"):
        regular_contents(worker)


def test_worker_scan_rejects_file_growth_during_read(tmp_path, monkeypatch):
    worker = tmp_path / "worker"
    worker.mkdir()
    target = worker / "file.txt"
    target.write_bytes(b"small")
    expected = target.lstat()
    original_fstat = os.fstat
    grown = False

    def grow_after_stat(fd):
        nonlocal grown
        metadata = original_fstat(fd)
        if not grown:
            grown = True
            target.write_bytes(b"x" * (1048576 + 100))
        return metadata

    monkeypatch.setattr(os, "fstat", grow_after_stat)
    with pytest.raises(ValueError, match="changed during"):
        _bounded_regular_read(target, expected)
    assert grown
