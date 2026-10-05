"""Opt-in stock-image observations, never blanket Fleet isolation qualification."""
from __future__ import annotations

import errno
import json
import os
import platform
from pathlib import Path

import pytest

PIN = json.loads((Path(__file__).parent / 'fixtures/native-receiver.json').read_text())
pytestmark = pytest.mark.skipif(
    os.environ.get('LONG_HAUL_REQUIRE_CONTAINER_SMOKE') != '1',
    reason='explicit bounded receiver container not supplied',
)


def record(name, value):
    root = Path(os.environ['LONG_HAUL_NATIVE_EVIDENCE'])
    (root / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n')


def test_container_has_effective_cpu_memory_swap_and_pid_limits():
    root = Path('/sys/fs/cgroup')
    values = {name: (root / name).read_text().strip()
              for name in ('cpu.max', 'memory.max', 'memory.swap.max', 'pids.max')}
    record('container-cgroup', values)
    quota, period = values['cpu.max'].split()
    assert int(quota) / int(period) == PIN['limits']['cores']
    assert int(values['memory.max']) == PIN['limits']['ramMiB'] * 1024**2
    assert values['memory.swap.max'] == '0'
    assert int(values['pids.max']) == PIN['limits']['pids']


def test_container_is_unprivileged_and_cannot_gain_privileges():
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines()
                  if ':' in line)
    observed = {'uid': os.getuid(), 'gid': os.getgid(),
                'CapEff': status['CapEff'].strip(), 'NoNewPrivs': status['NoNewPrivs'].strip(),
                'python': platform.python_version(), 'libc': platform.libc_ver()}
    record('container-process', observed)
    assert observed['uid'] != 0
    assert int(observed['CapEff'], 16) == 0
    assert observed['NoNewPrivs'] == '1'
    assert platform.python_version_tuple()[:2] == ('3', '12')
    assert not Path('/var/run/docker.sock').exists()
    assert not any(key in os.environ for key in ('GITHUB_TOKEN', 'ACTIONS_RUNTIME_TOKEN', 'AZURE_CLIENT_SECRET'))


@pytest.mark.parametrize('name', ['/etc/hostname', '/assets/inputs.json', '/source/pyproject.toml'])
def test_container_inputs_and_root_are_not_writable(name):
    # Opening for append writes no bytes even if unexpectedly permitted.
    try:
        with Path(name).open('ab'):
            pass
    except OSError as exc:
        record('container-readonly-' + Path(name).name, {'errno': exc.errno, 'path': name})
        assert exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM)
    else:
        pytest.fail('protected input was writable')


@pytest.mark.parametrize('path,key', [('/tmp', 'tmpdirMiB'), ('/out', 'outdirMiB')])
def test_container_scratch_and_output_have_bounded_filesystems(path, key):
    stats = os.statvfs(path)
    capacity = stats.f_blocks * stats.f_frsize
    record('container-storage-' + key, {'path': path, 'capacity_bytes': capacity})
    assert 0 < capacity <= PIN['limits'][key] * 1024**2
    probe = Path(path) / 'receiver-write-check'
    try:
        probe.write_bytes(b'bounded writable directory')
        assert probe.read_bytes() == b'bounded writable directory'
    finally:
        probe.unlink(missing_ok=True)
