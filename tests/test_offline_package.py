"""Check public package byte identity and safe archive boundaries."""

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_offline_package import (
    PackageError,
    extract_source,
    make_bundle,
)


def test_bundle_receipt_covers_exact_regular_members(tmp_path):
    sha = "a" * 40
    source = {
        "repository": "pH34r-pH/long-haul",
        "sha": sha,
        "ref": "refs/heads/main",
        "event": "push",
        "workflowPath": ".github/workflows/ci.yml",
        "job": "package",
        "runId": 123,
        "runAttempt": 1,
        "artifactName": f"longhaul-offline-package-{sha}-123-1",
    }
    runtime = {
        "pythonVersion": "3.11.16",
        "pythonMajorMinor": "3.11",
        "platform": "linux",
        "machine": "x86_64",
    }
    build_tools = {"pip": "24.3.1", "setuptools": "75.8.2", "wheel": "0.45.1"}
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    project_wheel = wheels / "long_haul-0.1.0-py3-none-any.whl"
    dependency_wheel = wheels / "PyYAML-6.0.3-cp311-cp311-manylinux.whl"
    project_wheel.write_bytes(b"project wheel bytes")
    dependency_wheel.write_bytes(b"dependency wheel bytes")
    package = {
        "name": "long-haul",
        "version": "0.1.0",
        "wheelPath": f"wheelhouse/{project_wheel.name}",
    }

    receipt = make_bundle(source, runtime, build_tools, package, wheels, tmp_path / "out")
    bundle = tmp_path / "out" / receipt["bundle"]["filename"]
    assert receipt["bundle"]["sha256"] == hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert receipt["files"] == sorted(receipt["files"], key=lambda item: item["path"])
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
        assert all(member.isfile() for member in members)
        assert sorted(member.name for member in members) == [item["path"] for item in receipt["files"]]
        for member, record in zip(
            sorted(members, key=lambda item: item.name), receipt["files"]
        ):
            content = archive.extractfile(member).read()
            assert len(content) == record["sizeBytes"]
            assert hashlib.sha256(content).hexdigest() == record["sha256"]
        manifest = json.loads(archive.extractfile("manifest.json").read())
    assert manifest["files"] == [item for item in receipt["files"] if item["path"] != "manifest.json"]
    assert manifest["source"] == receipt["source"]
    assert manifest["package"] == receipt["package"]


def test_bundle_rejects_missing_project_wheel_and_symlinks(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "dependency.whl").write_bytes(b"dependency")
    with pytest.raises(PackageError, match="Project wheel is absent"):
        make_bundle({"sha": "a" * 40}, {}, {}, {"wheelPath": "wheelhouse/project.whl"},
                    wheels, tmp_path / "out")
    (wheels / "project.whl").symlink_to(wheels / "dependency.whl")
    with pytest.raises(PackageError, match="linked wheel"):
        make_bundle({"sha": "a" * 40}, {}, {}, {"wheelPath": "wheelhouse/project.whl"},
                    wheels, tmp_path / "out")


def test_source_extractor_rejects_links_and_parent_paths(tmp_path):
    sha = "a" * 40
    archive = tmp_path / "source.tar"
    for name, kind in ((f"long-haul-{sha}/link", tarfile.SYMTYPE),
                       (f"long-haul-{sha}/../escape", tarfile.REGTYPE)):
        with tarfile.open(archive, "w") as tar:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
            member.size = 0
            tar.addfile(member, io.BytesIO())
        with pytest.raises(PackageError, match="unsafe path"):
            extract_source(archive, sha, tmp_path / "source")
