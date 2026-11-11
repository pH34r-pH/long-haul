"""Stopped byte snapshots reuse the exact existing Git/evaluator candidate model."""
from pathlib import Path

import pytest
from repository_evaluator_fixture import PROGRAM, git, setup
from test_harness_trajectory import reference

from long_haul.work.repository_candidate import freeze_repository_candidate
from long_haul.work.worker_candidate import _git, freeze_stopped_worker


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
