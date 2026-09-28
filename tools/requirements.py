#!/usr/bin/env python3
"""Validate declarative prerequisites and fingerprint their native sources.

CUE handles structure; this helper checks local file provenance. It never
installs packages, grants permissions or executes source-owned entrypoints.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

SCHEMA = Path(__file__).resolve().parents[1] / 'contracts/requirements.cue'
MAX_DECLARATION = 256 * 1024
MAX_MANIFEST = 16 * 1024 * 1024
MAX_MANIFESTS = 128
MAX_TOTAL_BYTES = 64 * 1024 * 1024


def safe_path(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or '\\' in name:
        raise ValueError('manifest path must be a nonempty POSIX relative path')
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(part in ('..', '.git') for part in relative.parts):
        raise ValueError('manifest path is outside the allowed source tree')
    base = root.resolve(strict=True)
    target = (base / relative).resolve(strict=True)
    if not target.is_relative_to(base) or not target.is_file():
        raise ValueError('manifest must be a regular file inside the source tree')
    if '.git' in target.relative_to(base).parts:
        raise ValueError('manifest must not resolve into Git metadata')
    return target


def read_bounded(path: Path, maximum: int) -> bytes:
    with path.open('rb') as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError('source input exceeds size limit')
    return content


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def load(root: Path) -> tuple[dict, bytes]:
    path = safe_path(root, '.fleet/requirements.json')
    raw = read_bounded(path, MAX_DECLARATION)
    data = json.loads(raw, object_pairs_hook=unique_object)
    return data, raw


def _python_native(content: bytes) -> dict:
    project = tomllib.loads(content.decode()).get('project', {})
    return {
        key: project[key]
        for key in ('requires-python', 'dependencies', 'optional-dependencies')
        if key in project
    }


def _node_native(content: bytes) -> dict:
    native = json.loads(content, object_pairs_hook=unique_object)
    return {
        key: native[key]
        for key in ('engines', 'packageManager', 'dependencies', 'devDependencies')
        if key in native
    }


def _rust_native(content: bytes) -> dict:
    package = tomllib.loads(content.decode()).get('package', {})
    return {
        key: package[key]
        for key in ('edition', 'rust-version')
        if key in package
    }


def _toolchain_native(content: bytes) -> dict:
    lines = [
        line.strip()
        for line in content.decode().splitlines()
        if line.strip() and not line.lstrip().startswith('#')
    ]
    if len(lines) != 1 or len(lines[0]) > 512:
        raise ValueError('expected one bounded native toolchain selection')
    return {'selection': lines[0]}


def native_manifest_record(path: Path, item: dict, content: bytes) -> dict:
    record = {'kind': item['kind'], 'sha256': hashlib.sha256(content).hexdigest()}
    readers = {
        'python-project': _python_native,
        'node-project': _node_native,
        'rust-project': _rust_native,
    }
    reader = readers.get(item['kind'])
    if reader is not None:
        record['native'] = reader(content)
    elif item['kind'] == 'toolchain' and path.name in {
        '.nvmrc', '.node-version', '.python-version', 'lean-toolchain', 'rust-toolchain'
    }:
        record['native'] = _toolchain_native(content)
    return record


def collect_manifests(root: Path, declarations: list[dict]) -> dict:
    if len(declarations) > MAX_MANIFESTS:
        raise ValueError('too many manifests or profiles')
    manifests = {}
    total_bytes = 0
    for item in declarations:
        name = item['path']
        if name in manifests:
            raise ValueError('duplicate manifest path')
        path = safe_path(root, name)
        content = read_bounded(path, MAX_MANIFEST)
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError('native manifest set exceeds total size limit')
        manifests[name] = native_manifest_record(path, item, content)
    return manifests


def validate_profiles(profiles: dict, manifests: dict) -> None:
    if not profiles:
        raise ValueError('at least one named profile is required')
    if len(profiles) > 128:
        raise ValueError('too many manifests or profiles')
    for name, profile in profiles.items():
        if not re.fullmatch(r'[a-z][a-z0-9-]*', name) or not profile['tools']:
            raise ValueError('profile names must be stable IDs and tools nonempty')
        if len(profile['tools']) > 256:
            raise ValueError('too many tool capabilities')
        for tool, need in profile['tools'].items():
            if not re.fullmatch(r'[a-z][a-z0-9+.-]*', tool):
                raise ValueError('invalid tool capability name')
            if any(source not in manifests for source in need['sources']):
                raise ValueError('tool source must reference a declared native manifest')


def inventory(root: Path, data: dict, raw: bytes) -> dict:
    manifests = collect_manifests(root, data['manifests'])
    validate_profiles(data['profiles'], manifests)
    return {
        'schemaVersion': 1,
        'repository': data['repository'],
        'requirementsSha256': hashlib.sha256(raw).hexdigest(),
        'manifests': manifests,
        'profiles': data['profiles'],
        'installed': False,
    }


def validate(root: Path) -> dict:
    data, raw = load(root)
    cue = shutil.which('cue')
    if cue is None:
        raise ValueError('CUE is required; install the pinned tool before validation')
    result = subprocess.run([cue, 'vet', '-c', str(SCHEMA), str(safe_path(root, '.fleet/requirements.json')), '-d', '#Requirements'], capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError('CUE requirements validation failed; run cue vet locally for details')
    return inventory(root, data, raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        report = validate(args.root.resolve())
        text = json.dumps(report, indent=2, sort_keys=True) + '\n'
        if args.output:
            args.output.write_text(text, encoding='utf-8')
        print(text, end='')
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f'Requirements check failed ({type(error).__name__}); verify CUE, declaration and source paths.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
