"""Build checked Long Haul runtime bytes on a public GitHub-hosted runner."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

REPOSITORY = "pH34r-pH/long-haul"
WORKFLOW_PATH = ".github/workflows/ci.yml"
TARGET = "longhaul-reference"
BUILD_TOOLS = ("pip==24.3.1", "setuptools==75.8.2", "wheel==0.45.1")
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_SOURCE_BYTES = 150 * 1024 * 1024
MAX_WHEEL_BYTES = 100 * 1024 * 1024
MAX_BUNDLE_BYTES = 350 * 1024 * 1024
MAX_WHEELS = 64


class PackageError(ValueError):
    """The build cannot produce a bounded, attributable offline package."""


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_identity(repo: Path, environment: dict[str, str]) -> dict:
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    if not SHA.fullmatch(sha) or environment.get("GITHUB_SHA") != sha:
        raise PackageError("Checkout does not match the exact GitHub source SHA")
    if environment.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise PackageError("Package producer is not the public Long Haul repository")
    if environment.get("GITHUB_EVENT_NAME") != "push" or environment.get("GITHUB_REF") != "refs/heads/main":
        raise PackageError("Only a trusted main push may publish a package")
    if subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain"], text=True
    ).strip():
        raise PackageError("Package checkout must be clean before archiving")
    run_id = environment.get("GITHUB_RUN_ID", "")
    run_attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
    if not run_id.isdecimal() or not run_attempt.isdecimal() or int(run_id) < 1 or int(run_attempt) < 1:
        raise PackageError("GitHub run ID and attempt must be positive integers")
    return {
        "repository": REPOSITORY,
        "sha": sha,
        "ref": "refs/heads/main",
        "event": "push",
        "workflowPath": WORKFLOW_PATH,
        "job": "package",
        "runId": int(run_id),
        "runAttempt": int(run_attempt),
        "artifactName": f"longhaul-offline-package-{sha}-{run_id}-{run_attempt}",
    }


def archive_tracked_source(repo: Path, sha: str, destination: Path) -> None:
    with destination.open("xb") as output:
        subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar",
             f"--prefix=long-haul-{sha}/", sha],
            stdout=output, check=True,
        )


def extract_source(archive: Path, sha: str, destination: Path) -> Path:
    """Copy only regular tracked files from the exact commit, without tar links."""
    root_name = f"long-haul-{sha}"
    seen: set[str] = set()
    total = 0
    with tarfile.open(archive, "r:") as source:
        for item in source:
            parts = PurePosixPath(item.name).parts
            name = "/".join(parts)
            if (not parts or parts[0] != root_name or ".." in parts
                    or item.name.startswith("/") or "\\" in item.name
                    or name in seen or not (item.isfile() or item.isdir())):
                raise PackageError("Source archive has an unsafe path, duplicate or link")
            seen.add(name)
            target = destination.joinpath(*parts)
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += item.size
            if total > MAX_SOURCE_BYTES:
                raise PackageError("Expanded source exceeds its size limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            member = source.extractfile(item)
            if member is None:
                raise PackageError("Source archive member is unreadable")
            with member, target.open("xb") as output:
                shutil.copyfileobj(member, output)
    root = destination / root_name
    for required in ("pyproject.toml", "src/long_haul/__init__.py"):
        if not (root / required).is_file():
            raise PackageError(f"Tracked source lacks {required}")
    return root


def file_record(path: Path, relative: str) -> dict:
    parsed = PurePosixPath(relative)
    if (parsed.is_absolute() or ".." in parsed.parts or "\\" in relative
            or str(parsed) != relative):
        raise PackageError("Package member path is not canonical")
    if not path.is_file() or path.is_symlink():
        raise PackageError(f"Package member is missing, linked or not regular: {relative}")
    size = path.stat().st_size
    if not 0 < size <= MAX_WHEEL_BYTES:
        raise PackageError(f"Package member exceeds its size limit: {relative}")
    return {"path": relative, "sha256": digest_file(path), "sizeBytes": size}


def json_bytes(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def tar_member(bundle: tarfile.TarFile, name: str, data: bytes | Path) -> None:
    if isinstance(data, Path):
        info = tarfile.TarInfo(name)
        info.size = data.stat().st_size
        stream = data.open("rb")
    else:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        stream = io.BytesIO(data)
    info.mode = 0o644
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    with stream:
        bundle.addfile(info, stream)


def verify_project_wheel(path: Path, name: str, version: str) -> None:
    try:
        with zipfile.ZipFile(path) as wheel:
            members = wheel.namelist()
            if len(members) != len(set(members)):
                raise PackageError("Project wheel contains duplicate member paths")
            if any(n.startswith("/") or "\\" in n or ".." in PurePosixPath(n).parts for n in members):
                raise PackageError("Project wheel contains an unsafe member path")
            metadata_paths = [n for n in members if n.endswith(".dist-info/METADATA")]
            if len(metadata_paths) != 1:
                raise PackageError("Project wheel has no unique METADATA")
            metadata = BytesParser().parsebytes(wheel.read(metadata_paths[0]))
    except zipfile.BadZipFile as exc:
        raise PackageError("Project wheel is not a valid ZIP") from exc
    if metadata.get("Name", "").lower().replace("_", "-") != name or metadata.get("Version") != version:
        raise PackageError("Project wheel metadata differs from checked pyproject.toml")


def make_bundle(source: dict, runtime: dict, build_tools: dict, package: dict,
                wheels: Path, output_dir: Path) -> dict:
    wheel_files = sorted(wheels.glob("*.whl"))
    if not wheel_files or len(wheel_files) > MAX_WHEELS:
        raise PackageError("Offline wheelhouse is empty or too large")
    if any(p.is_symlink() for p in wheel_files):
        raise PackageError("Offline wheelhouse contains a linked wheel")
    wheel_records = [
        file_record(wheel, f"wheelhouse/{wheel.name}") for wheel in wheel_files
    ]
    if [item["path"] for item in wheel_records] != sorted({item["path"] for item in wheel_records}):
        raise PackageError("Offline wheelhouse paths are duplicated or unordered")
    if package["wheelPath"] not in {item["path"] for item in wheel_records}:
        raise PackageError("Project wheel is absent from offline wheelhouse")
    common = {
        "schemaVersion": 1,
        "kind": "long-haul.offline-package",
        "state": "built-on-public-runner-no-deployment",
        "target": TARGET,
        "source": source,
        "runtime": runtime,
        "build": build_tools,
        "package": package,
    }
    manifest = {**common, "files": wheel_records}
    manifest_data = json_bytes(manifest)
    manifest_record = {
        "path": "manifest.json",
        "sha256": hashlib.sha256(manifest_data).hexdigest(),
        "sizeBytes": len(manifest_data),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = output_dir / f"longhaul-{source['sha']}-offline.tar.gz"
    with (
        bundle_path.open("xb") as output,
        gzip.GzipFile(filename="", mode="wb", mtime=0, fileobj=output) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as bundle,
    ):
        tar_member(bundle, "manifest.json", manifest_data)
        for wheel in wheel_files:
            tar_member(bundle, f"wheelhouse/{wheel.name}", wheel)
    if bundle_path.stat().st_size > MAX_BUNDLE_BYTES:
        bundle_path.unlink()
        raise PackageError("Offline bundle exceeds its size limit")
    receipt = {
        **common,
        "files": sorted([manifest_record, *wheel_records], key=lambda item: item["path"]),
        "bundle": {
            "filename": bundle_path.name,
            "sha256": digest_file(bundle_path),
            "sizeBytes": bundle_path.stat().st_size,
        },
    }
    (output_dir / "package-receipt.json").write_bytes(json_bytes(receipt))
    return receipt


def build(repo: Path, output_dir: Path, environment: dict[str, str]) -> dict:
    source = source_identity(repo, environment)
    if sys.version_info[:2] != (3, 11) or sys.platform != "linux" or platform.machine() != "x86_64":
        raise PackageError("Long Haul offline package requires Python 3.11 on Linux x86_64")
    with tempfile.TemporaryDirectory(prefix="longhaul-public-package-") as temporary:
        work = Path(temporary)
        archive = work / "source.tar"
        archive_tracked_source(repo, source["sha"], archive)
        source_root = extract_source(archive, source["sha"], work / "source")
        project = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        if project["name"] != "long-haul":
            raise PackageError("Tracked pyproject.toml is not the Long Haul package")
        version = project["version"]
        wheelhouse = work / "wheels"
        wheelhouse.mkdir()
        build_venv = work / "build-venv"
        subprocess.run([sys.executable, "-m", "venv", str(build_venv)], check=True)
        python = str(build_venv / "bin/python")
        subprocess.run([
            python, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-cache-dir", *BUILD_TOOLS,
        ], check=True)
        subprocess.run([
            python, "-m", "pip", "wheel", "--disable-pip-version-check",
            "--no-cache-dir", "--no-build-isolation", "--only-binary=:all:",
            "--wheel-dir", str(wheelhouse), str(source_root),
        ], check=True)
        project_wheels = list(wheelhouse.glob(f"long_haul-{version}-*.whl"))
        if len(project_wheels) != 1:
            raise PackageError("Expected exactly one Long Haul project wheel")
        verify_project_wheel(project_wheels[0], "long-haul", version)
        verify_venv = work / "verify-venv"
        subprocess.run([sys.executable, "-m", "venv", str(verify_venv)], check=True)
        verify_python = str(verify_venv / "bin/python")
        subprocess.run([
            verify_python, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-index", "--find-links", str(wheelhouse), "--force-reinstall",
            f"long-haul=={version}",
        ], check=True)
        subprocess.run([verify_python, "-m", "pip", "check"], check=True)
        subprocess.run([verify_python, "-c", "import long_haul; assert long_haul.Vessel"], check=True)
        tool_versions = json.loads(subprocess.check_output([
            python, "-c",
            "import importlib.metadata as m, json; print(json.dumps({n:m.version(n) for n in ('pip','setuptools','wheel')}))",
        ], text=True))
        if tool_versions != {"pip": "24.3.1", "setuptools": "75.8.2", "wheel": "0.45.1"}:
            raise PackageError("Build tools differ from the pinned producer versions")
        runtime = {
            "pythonVersion": platform.python_version(),
            "pythonMajorMinor": "3.11",
            "platform": "linux",
            "machine": "x86_64",
        }
        package = {
            "name": "long-haul",
            "version": version,
            "wheelPath": f"wheelhouse/{project_wheels[0].name}",
        }
        return make_bundle(source, runtime, tool_versions, package, wheelhouse, output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    result = build(Path(__file__).resolve().parent.parent, args.output_dir, os.environ)
    print(f"Checked offline Long Haul package: {result['bundle']['sha256']}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        raise SystemExit(f"Long Haul offline package failed: {exc}") from exc
