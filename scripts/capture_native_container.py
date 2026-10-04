"""Export bounded smoke observations before container tmpfs is destroyed.

This is a qualification fixture, not an experiment executor or receipt format.
The controller stores the original file bytes; this envelope is transport only.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import resource
import sys

import pytest

MAX_FILE = 2 * 1024 ** 2
MAX_TOTAL = 4 * 1024 ** 2


def observation_files(root: Path) -> dict:
    files = {}
    total = 0
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
            raise ValueError('unexpected observation entry')
        raw = path.read_bytes()
        total += len(raw)
        if total > MAX_TOTAL:
            raise ValueError('observation total exceeds transport budget')
        files[path.name] = {'sha256': hashlib.sha256(raw).hexdigest(),
                            'base64': base64.b64encode(raw).decode('ascii')}
    return files


def main() -> int:
    root = Path('/evidence')
    root.mkdir(exist_ok=True)
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE, MAX_FILE))
    status = 2
    with (root / 'container-smoke.log').open('w', encoding='utf-8') as log:
        previous_out, previous_err = sys.stdout, sys.stderr
        try:
            sys.stdout = sys.stderr = log
            status = int(pytest.main([
                '-q', '-s', '-p', 'no:cacheprovider',
                'tests/test_llama_container_boundary.py',
                'tests/test_llama_native_completion.py', 'tests/test_llama_native_server.py',
                '--junitxml=/evidence/container-junit.xml',
            ]))
        finally:
            sys.stdout, sys.stderr = previous_out, previous_err
    usage = {}
    for name in ('memory.peak', 'memory.events', 'cpu.stat', 'pids.peak'):
        source = Path('/sys/fs/cgroup') / name
        usage[name] = source.read_text().strip() if source.is_file() else None
    (root / 'container-usage.json').write_text(json.dumps(usage, sort_keys=True) + '\n')
    print(json.dumps({'exit_code': status, 'files': observation_files(root)}, sort_keys=True))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
