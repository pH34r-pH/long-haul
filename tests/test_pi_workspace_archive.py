"""Archive validation is a component test, not a sandbox qualification."""
import io
import tarfile

import pytest

from long_haul.adapters.pi_workspace_archive import (
    MAX_ARCHIVE_BYTES,
    read_workspace_archive,
)


def archive_of(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name, kind, data in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.REGTYPE:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            else:
                info.linkname = data.decode() if data else ""
                archive.addfile(info)
    return output.getvalue()


ROOT = (".", tarfile.DIRTYPE, b"")


def test_workspace_archive_reads_regular_files_without_host_extraction(tmp_path):
    content = archive_of([ROOT, ("./nested", tarfile.DIRTYPE, b""),
                          ("./nested/a.txt", tarfile.REGTYPE, b"fixture")])
    assert read_workspace_archive(io.BytesIO(content)) == {"nested/a.txt": b"fixture"}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("entry", [
    ("../escape", tarfile.REGTYPE, b"x"),
    ("./../escape", tarfile.REGTYPE, b"x"),
    ("./.git/config", tarfile.REGTYPE, b"x"),
    ("./.pi/settings", tarfile.REGTYPE, b"x"),
    ("./link", tarfile.SYMTYPE, b"/etc/passwd"),
    ("./link", tarfile.LNKTYPE, b"./a"),
    ("./fifo", tarfile.FIFOTYPE, b""),
    ("./a\\b", tarfile.REGTYPE, b"x"),
])
def test_workspace_archive_rejects_unsafe_entries(entry):
    with pytest.raises(ValueError):
        read_workspace_archive(io.BytesIO(archive_of([ROOT, entry])))


@pytest.mark.parametrize("entries", [
    [("./a", tarfile.REGTYPE, b"x")],
    [ROOT, ROOT],
    [ROOT, ("./a", tarfile.REGTYPE, b"x"), ("./a", tarfile.REGTYPE, b"y")],
    [ROOT, ("./a", tarfile.REGTYPE, b"x"), ("./a/b", tarfile.REGTYPE, b"y")],
    [ROOT, ("./missing/a", tarfile.REGTYPE, b"x")],
])
def test_workspace_archive_rejects_missing_root_duplicates_and_file_ancestors(entries):
    with pytest.raises(ValueError):
        read_workspace_archive(io.BytesIO(archive_of(entries)))


def test_workspace_archive_limits_files_and_total_size():
    content = b"x" * 1048576
    entries = [ROOT] + [(f"./{index}", tarfile.REGTYPE, content) for index in range(5)]
    with pytest.raises(ValueError, match="candidate bounds"):
        read_workspace_archive(io.BytesIO(archive_of(entries)))
    with pytest.raises(ValueError, match="oversized"):
        read_workspace_archive(io.BytesIO(archive_of([ROOT, ("./large", tarfile.REGTYPE, content + b"x")])))


def test_workspace_archive_limits_entry_counts_and_depth():
    files = [ROOT] + [(f"./f{index}", tarfile.REGTYPE, b"") for index in range(257)]
    with pytest.raises(ValueError, match="candidate bounds"):
        read_workspace_archive(io.BytesIO(archive_of(files)))
    directories = [ROOT] + [(f"./d{index}", tarfile.DIRTYPE, b"") for index in range(257)]
    with pytest.raises(ValueError, match="directory count"):
        read_workspace_archive(io.BytesIO(archive_of(directories)))
    with pytest.raises(ValueError, match="directory depth"):
        read_workspace_archive(io.BytesIO(archive_of([ROOT, ("./" + "d/" * 64 + "a", tarfile.REGTYPE, b"")])))


def test_workspace_archive_bounds_trailing_bytes_and_rejects_truncation():
    valid = archive_of([ROOT, ("./a", tarfile.REGTYPE, b"x")])
    with pytest.raises(ValueError, match="byte limit"):
        read_workspace_archive(io.BytesIO(valid + b"x" * MAX_ARCHIVE_BYTES))
    with pytest.raises(ValueError):
        read_workspace_archive(io.BytesIO(valid[:700]))
