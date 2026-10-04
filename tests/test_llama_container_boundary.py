"""Kernel-observed checks inside the explicit qualification container only."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import socket

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get('LONG_HAUL_REQUIRE_CONTAINER_BOUNDARY') != '1',
    reason='explicit isolated-container qualification required',
)


def save(name, value):
    Path('/evidence', name + '.json').write_text(json.dumps(value, sort_keys=True) + '\n')


def test_no_egress_but_private_loopback_is_available():
    interfaces = sorted(path.name for path in Path('/sys/class/net').iterdir())
    assert interfaces == ['lo']
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname(), timeout=1) as client:
            peer, _ = listener.accept()
            with peer:
                client.sendall(b'local-only')
                assert peer.recv(10) == b'local-only'
    with socket.socket() as external:
        external.settimeout(1)
        with pytest.raises(OSError) as denied:
            external.connect(('192.0.2.1', 9))
    # Timeout/refusal is not proof of no route. Require the explicit kernel error.
    assert denied.value.errno == errno.ENETUNREACH
    save('boundary-network', {'interfaces': interfaces, 'loopback': True,
                              'external_errno': denied.value.errno})


def test_kernel_resource_and_privilege_limits():
    root = Path('/sys/fs/cgroup')
    observed = {key: (root / key).read_text().strip()
                for key in ('memory.max', 'memory.swap.max', 'pids.max', 'cpu.max')}
    assert int(observed['memory.max']) == 1024 ** 3
    assert int(observed['memory.swap.max']) == 0
    assert int(observed['pids.max']) == 96
    quota, period = map(int, observed['cpu.max'].split())
    assert quota == period and quota > 0
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines()
                  if ':' in line)
    assert os.getuid() == 65534
    assert int(status['CapEff'].strip(), 16) == 0
    assert status['NoNewPrivs'].strip() == '1'
    assert status['Seccomp'].strip() == '2'
    observed.update(uid=os.getuid(), capabilities=status['CapEff'].strip(),
                    no_new_privileges=status['NoNewPrivs'].strip(),
                    seccomp=status['Seccomp'].strip())
    save('boundary-resources', observed)


def test_readonly_inputs_and_bounded_evidence_filesystem():
    errors = {}
    for path in ('/forbidden-write', '/inputs/model.gguf'):
        with pytest.raises(OSError) as denied:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(descriptor)
        assert denied.value.errno == errno.EROFS
        errors[path] = denied.value.errno
    assert not Path('/var/run/docker.sock').exists()
    assert not any(key.startswith(('GITHUB_', 'ACTIONS_', 'AZURE_')) for key in os.environ)
    stats = os.statvfs('/evidence')
    assert stats.f_blocks * stats.f_frsize == 16 * 1024 ** 2
    save('boundary-filesystem', {'write_errors': errors, 'evidence_capacity_bytes':
                                 stats.f_blocks * stats.f_frsize})
