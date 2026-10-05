"""Opt-in Linux loopback scope checks alongside the existing native-model smoke.

CWL NetworkAccess=false permits localhost. These observations establish the
selected network namespace, not filesystem isolation or a qualified Fleet lease.
"""
import errno
import json
import os
import socket
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get('LONG_HAUL_REQUIRE_NETNS_SMOKE') != '1',
    reason='explicit isolated native smoke not requested',
)


def record_scope(name, observation):
    root = Path(os.environ['LONG_HAUL_NATIVE_EVIDENCE'])
    root.mkdir(parents=True, exist_ok=True)
    (root / f'{name}.json').write_text(
        json.dumps(observation, indent=2, sort_keys=True) + '\n', encoding='utf-8',
    )


def test_network_namespace_is_distinct_and_loopback_only():
    parent = os.environ['LONG_HAUL_PARENT_NETNS']
    current = os.readlink('/proc/self/ns/net')
    interfaces = [name for _, name in socket.if_nameindex()]
    record_scope('network-namespace', {
        'parent_network_namespace': parent, 'current_network_namespace': current,
        'interfaces': interfaces,
        'scope': 'Linux network namespace only; not filesystem or Fleet qualification',
    })
    assert parent and current != parent
    assert interfaces == ['lo']


def test_localhost_roundtrip_is_available_without_external_network():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(2)
        with socket.create_connection(listener.getsockname(), timeout=2) as client:
            peer, _ = listener.accept()
            with peer:
                peer.settimeout(2)
                client.sendall(b'loopback-scope')
                assert peer.recv(64) == b'loopback-scope'
                peer.sendall(b'ack')
                assert client.recv(64) == b'ack'
    record_scope('network-loopback', {'roundtrip': 'passed', 'address': '127.0.0.1'})


def test_non_loopback_connect_fails_with_no_route_not_a_timeout():
    # TEST-NET-1; there is no non-loopback interface in this namespace. Check the
    # topology before connecting so accidental unisolated invocation sends nothing.
    assert [name for _, name in socket.if_nameindex()] == ['lo']
    with socket.socket() as client:
        client.settimeout(2)
        try:
            client.connect(('192.0.2.1', 9))
        except OSError as error:
            record_scope('network-outbound', {
                'address': '192.0.2.1', 'errno': error.errno,
                'error_type': type(error).__name__,
                'expected_errno': errno.ENETUNREACH,
            })
            assert error.errno == errno.ENETUNREACH
        else:
            pytest.fail('non-loopback connection unexpectedly succeeded')
