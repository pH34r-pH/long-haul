"""Seed archive component tests; these do not qualify an actual worker."""
import io
import tarfile

import pytest

from long_haul.adapters.pi_workspace_seed import build_workspace_seed


def test_seed_contains_only_projected_bytes_and_missing_target_parents():
    targets = {"fixture/current.py", "fixture/new/file.py"}
    data = {"fixture/current.py": b"content"}
    seed = build_workspace_seed(targets, data)
    assert seed == build_workspace_seed(sorted(targets, reverse=True), data)
    with tarfile.open(fileobj=io.BytesIO(seed), mode="r:") as archive:
        entries = {item.name: item for item in archive}
        assert set(entries) == {"fixture", "fixture/new", "fixture/current.py"}
        assert entries["fixture"].isdir() and entries["fixture"].mode == 0o777
        assert entries["fixture/new"].isdir() and entries["fixture/new"].mode == 0o777
        assert entries["fixture/current.py"].isfile() and entries["fixture/current.py"].mode == 0o666
        assert archive.extractfile(entries["fixture/current.py"]).read() == b"content"


@pytest.mark.parametrize("targets,contents", [
    ({"safe"}, {"outside": b"x"}),
    ({"../escape"}, {}),
    ({".git/config"}, {}),
    ({"a", "a/b"}, {}),
    ({"a"}, {"a": b"x" * (1048576 + 1)}),
    ({f"f{index}" for index in range(257)}, {}),
    ({"d/" * 64 + "f"}, {}),
])
def test_seed_rejects_unsafe_or_oversized_input(targets, contents):
    with pytest.raises(ValueError):
        build_workspace_seed(targets, contents)


def test_seed_rejects_excessive_aggregate_bytes():
    contents = {f"f{index}": b"x" * 1048576 for index in range(5)}
    with pytest.raises(ValueError, match="total bound"):
        build_workspace_seed(set(contents), contents)
