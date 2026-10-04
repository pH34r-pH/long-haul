"""Qualify the existing tiny native smoke in a disposable Docker boundary.

Not a Fleet dispatcher or generic package executor. No credentials, ports, model
acquisition or infrastructure provisioning are owned by this fixture.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

MAX_TRANSPORT = 6 * 1024 ** 2
MAX_FILE = 2 * 1024 ** 2
MAX_TOTAL = 4 * 1024 ** 2


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')


def docker(*arguments: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(['docker', *arguments], stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=timeout, check=False)


def checked(*arguments: str) -> bytes:
    result = docker(*arguments)
    if result.returncode:
        raise RuntimeError('Docker qualification command failed: ' + result.stderr.decode()[-1000:])
    return result.stdout


def container_arguments(image: str, model: Path, tokenizer: Path) -> list[str]:
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('qualification requires the built image ID')
    for path in (model, tokenizer):
        if not path.is_absolute() or ',' in str(path):
            raise ValueError('invalid mounted fixture path')
    return ['create', '--init', '--network=none', '--read-only', '--user=65534:65534',
            '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=96',
            '--memory=1073741824', '--memory-swap=1073741824', '--cpus=1',
            '--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=134217728,mode=1777',
            '--tmpfs=/evidence:rw,noexec,nosuid,nodev,size=16777216,mode=1777',
            '--log-driver=local', '--log-opt=max-size=8m', '--log-opt=max-file=1',
            '--mount', f'type=bind,source={model},target=/inputs/model.gguf,readonly',
            '--mount', f'type=bind,source={tokenizer},target=/inputs/tokenizer,readonly']


def observation_payload(raw: bytes) -> tuple[int, dict[str, bytes]]:
    if len(raw) > MAX_TRANSPORT:
        raise ValueError('observation transport too large')
    envelope = json.loads(raw)
    if set(envelope) != {'exit_code', 'files'} or type(envelope['exit_code']) is not int:
        raise ValueError('invalid observation envelope')
    records = envelope['files']
    if not isinstance(records, dict) or not 1 <= len(records) <= 64:
        raise ValueError('invalid observation file count')
    files = {}
    for name, entry in records.items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name):
            raise ValueError('unsafe observation filename')
        if not isinstance(entry, dict) or set(entry) != {'base64', 'sha256'}:
            raise ValueError('invalid observation entry')
        data = base64.b64decode(entry['base64'], validate=True)
        if len(data) > MAX_FILE or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError('observation size or digest mismatch')
        files[name] = data
    if sum(map(len, files.values())) > MAX_TOTAL:
        raise ValueError('observation total too large')
    return envelope['exit_code'], files


def inspect_container(identifier: str):
    return json.loads(checked('inspect', identifier))[0]


def remove_container(identifier: str, root: Path) -> None:
    before = inspect_container(identifier)
    write_json(root / 'before-removal.json', {'state': before['State']})
    checked('rm', '--force', identifier)
    remaining = checked('ps', '-aq', '--no-trunc', '--filter', f'id={identifier}').strip()
    write_json(root / 'cleanup.json', {'container_id': identifier, 'removed': not remaining})
    if remaining:
        raise RuntimeError('owned container remains after removal')


def verify_boundary(inspect: dict) -> None:
    config = inspect['HostConfig']
    if (config['NetworkMode'] != 'none' or not config['ReadonlyRootfs']
            or config['Memory'] != 1024 ** 3 or config['MemorySwap'] != 1024 ** 3
            or config['NanoCpus'] != 10 ** 9 or config['PidsLimit'] != 96
            or config.get('Privileged') or config.get('PortBindings')):
        raise ValueError('created container boundary differs from requested fixture')
    if inspect['Config']['User'] != '65534:65534':
        raise ValueError('wrong fixture user')
    mounts = {entry['Destination']: entry for entry in inspect['Mounts']
              if entry['Type'] == 'bind'}
    if set(mounts) != {'/inputs/model.gguf', '/inputs/tokenizer'}:
        raise ValueError('unexpected host bind mount')
    if any(entry['RW'] for entry in mounts.values()):
        raise ValueError('fixture inputs must be read only')


def run_smoke(arguments: list[str], image: str, root: Path) -> None:
    root.mkdir()
    identifier = checked(*arguments, image).decode().strip()
    if not re.fullmatch('[0-9a-f]{64}', identifier):
        raise ValueError('invalid created container ID')
    (root / 'container.id').write_text(identifier + '\n')
    try:
        snapshot = inspect_container(identifier)
        write_json(root / 'created.json', snapshot)
        verify_boundary(snapshot)
        started = time.monotonic()
        result = docker('start', '--attach', identifier, timeout=300)
        (root / 'transport.stdout').write_bytes(result.stdout)
        (root / 'transport.stderr').write_bytes(result.stderr)
        write_json(root / 'process.json', {'exit_code': result.returncode,
                                          'elapsed_seconds': time.monotonic() - started})
        status, files = observation_payload(result.stdout)
        target = root / 'observations'
        target.mkdir()
        for name, data in files.items():
            (target / name).write_bytes(data)
        if status != 0 or result.returncode != status:
            raise RuntimeError('isolated native smoke failed; original observations retained')
    except subprocess.TimeoutExpired as error:
        write_json(root / 'timeout.json', {'timeout_seconds': error.timeout})
        raise
    finally:
        remove_container(identifier, root)


def cancellation_probe(arguments: list[str], image: str, root: Path) -> None:
    root.mkdir()
    program = ("import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c',"
               "'import time; time.sleep(60)']); print(p.pid,flush=True); time.sleep(60)")
    identifier = checked(*arguments, '--entrypoint=/venv/bin/python', image,
                         '-c', program).decode().strip()
    (root / 'container.id').write_text(identifier + '\n')
    try:
        snapshot = inspect_container(identifier)
        verify_boundary(snapshot)
        write_json(root / 'created.json', snapshot)
        try:
            docker('start', '--attach', identifier, timeout=3)
        except subprocess.TimeoutExpired:
            top = checked('top', identifier).decode()
            (root / 'process-tree.txt').write_text(top)
            # Observe init, supervisor and its actual child before force-removal.
            if len(top.strip().splitlines()) < 4:
                raise RuntimeError('descendant was not observed before cancellation')
            write_json(root / 'timeout.json', {'timeout_seconds': 3, 'descendant_observed': True})
        else:
            raise RuntimeError('cancellation fixture exited before the timeout')
    finally:
        remove_container(identifier, root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--model', required=True, type=Path)
    parser.add_argument('--tokenizer', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    model, tokenizer = args.model.resolve(strict=True), args.tokenizer.resolve(strict=True)
    if not model.is_file() or not tokenizer.is_dir():
        parser.error('regular model and tokenizer directory required')
    arguments = container_arguments(args.image, model, tokenizer)
    args.output.mkdir(parents=True, exist_ok=True)
    images = json.loads(checked('image', 'inspect', args.image))
    if len(images) != 1 or images[0]['Id'] != args.image:
        raise ValueError('built image identity mismatch')
    write_json(args.output / 'image.json', images[0])
    checked('version')
    run_smoke(arguments, args.image, args.output / 'smoke')
    cancellation_probe(arguments, args.image, args.output / 'cancellation')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
