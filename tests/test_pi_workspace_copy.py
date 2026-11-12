"""Exercise the subprocess transport; no Docker or sandbox qualification."""
import base64
import io
import os
import tarfile
import time

import pytest

from long_haul.adapters.pi_workspace_copy import copy_docker_workspace

pytestmark = pytest.mark.skipif(os.name != "posix", reason="worker transport requires POSIX pipes")
CONTAINER = "a" * 64


def _stub(tmp_path, *, action):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        item = tarfile.TarInfo("./file")
        item.size = 7
        tar.addfile(item, io.BytesIO(b"fixture"))
    executable = tmp_path / "docker-stub"
    executable.write_text(
        "#!/usr/bin/env python3\nimport base64,sys,time\n"
        "assert sys.argv[-3:] == ['cp', '" + CONTAINER + ":/workspace/.', '-']\n"
        "data = base64.b64decode('" + base64.b64encode(archive.getvalue()).decode() + "')\n"
        + action + "\n"
    )
    executable.chmod(0o700)
    return executable


def test_copy_accepts_only_successful_subprocess_and_bounded_archive(tmp_path):
    tool = _stub(tmp_path, action="sys.stdout.buffer.write(data); sys.stdout.flush()")
    assert copy_docker_workspace(tool, CONTAINER, 5) == {"file": b"fixture"}
    with pytest.raises(ValueError, match="invalid workspace copy"):
        copy_docker_workspace(tool, "bad-container", 5)


def test_copy_rejects_valid_archive_if_subprocess_fails(tmp_path):
    tool = _stub(tmp_path, action="sys.stdout.buffer.write(data); sys.stdout.flush(); sys.exit(3)")
    with pytest.raises(ValueError, match="did not finish successfully"):
        copy_docker_workspace(tool, CONTAINER, 5)


def test_copy_terminates_stalled_subprocess_at_deadline(tmp_path):
    tool = _stub(tmp_path, action="time.sleep(30)")
    start = time.monotonic()
    with pytest.raises(ValueError):
        copy_docker_workspace(tool, CONTAINER, 0.2)
    assert time.monotonic() - start < 5


def test_copy_rejects_unbounded_archive_stream(tmp_path):
    tool = _stub(tmp_path, action="sys.stdout.buffer.write(data + b'\\0' * (9 * 1024 * 1024)); sys.stdout.flush()")
    with pytest.raises(ValueError, match="byte limit"):
        copy_docker_workspace(tool, CONTAINER, 5)
