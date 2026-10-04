"""Offline tests for the qualification fixture's command and evidence boundary."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

PATH = Path(__file__).parents[1] / 'scripts' / 'check_native_container.py'
SPEC = importlib.util.spec_from_file_location('native_container_contract', PATH)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


def payload(data=b'original bytes\n', name='observation.json'):
    return {'exit_code': 0, 'files': {name: {'sha256': hashlib.sha256(data).hexdigest(),
                                           'base64': base64.b64encode(data).decode('ascii')}}}


def snapshot():
    return {'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True,
                           'Memory': 1024 ** 3, 'MemorySwap': 1024 ** 3,
                           'NanoCpus': 10 ** 9, 'PidsLimit': 96,
                           'Privileged': False, 'PortBindings': {}},
            'Config': {'User': '65534:65534'},
            'Mounts': [{'Type': 'bind', 'Destination': path, 'RW': False}
                       for path in ('/inputs/model.gguf', '/inputs/tokenizer')]}


def test_no_host_network_ports_capabilities_or_socket_mount_in_command():
    args = m.container_arguments('sha256:' + 'a' * 64, Path('/input/model'), Path('/input/tokenizer'))
    assert '--network=none' in args and '--read-only' in args
    assert '--cap-drop=ALL' in args and '--security-opt=no-new-privileges' in args
    assert '--log-opt=max-file=1' in args and '--log-opt=compress=false' in args
    assert '--memory=1073741824' in args and '--memory-swap=1073741824' in args
    assert not any('docker.sock' in item for item in args)
    assert sum(item == '--mount' for item in args) == 2
    assert not {'--privileged', '--publish', '-p', '-v', '--env-file'} & set(args)


@pytest.mark.parametrize('image', ['ubuntu:24.04', 'latest', '', 'sha256:' + 'z' * 64])
def test_mutable_or_invalid_image_rejected(image):
    with pytest.raises(ValueError):
        m.container_arguments(image, Path('/a'), Path('/b'))


@pytest.mark.parametrize('path', [Path('relative'), Path('/a,readonly=false')])
def test_mount_injection_rejected(path):
    with pytest.raises(ValueError):
        m.container_arguments('sha256:' + 'a' * 64, path, Path('/b'))


def test_original_bytes_are_preserved_and_hash_checked():
    raw = 'café\n\n'.encode()
    status, files = m.observation_payload(json.dumps(payload(raw)).encode())
    assert status == 0 and files['observation.json'] == raw


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a/b', 'a\\b', '.', '..', ''])
def test_unsafe_returned_path_rejected(name):
    with pytest.raises(ValueError):
        m.observation_payload(json.dumps(payload(name=name)).encode())


def test_tampered_bytes_rejected():
    value = payload()
    value['files']['observation.json']['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='digest'):
        m.observation_payload(json.dumps(value).encode())


def test_oversized_transport_rejected_before_json():
    with pytest.raises(ValueError, match='transport'):
        m.observation_payload(b' ' * (m.MAX_TRANSPORT + 1))


def test_original_failure_status_is_not_changed_to_success():
    value = payload()
    value['exit_code'] = 1
    assert m.observation_payload(json.dumps(value).encode())[0] == 1


@pytest.mark.parametrize('key,value', [('NetworkMode', 'host'), ('ReadonlyRootfs', False),
                                      ('Memory', 0), ('MemorySwap', -1), ('NanoCpus', 0),
                                      ('PidsLimit', 0), ('Privileged', True),
                                      ('PortBindings', {'8000/tcp': [{'HostPort': '8000'}]})])
def test_incorrect_created_boundaries_fail_before_start(key, value):
    actual = snapshot()
    actual['HostConfig'][key] = value
    with pytest.raises(ValueError):
        m.verify_boundary(actual)


def test_readonly_mounts_and_user_checked():
    actual = snapshot()
    m.verify_boundary(actual)
    modified = deepcopy(actual)
    modified['Config']['User'] = 'root'
    with pytest.raises(ValueError):
        m.verify_boundary(modified)
    actual['Mounts'][0]['RW'] = True
    with pytest.raises(ValueError):
        m.verify_boundary(actual)


def test_extra_host_mount_rejected():
    actual = snapshot()
    actual['Mounts'].append({'Type': 'bind', 'Destination': '/host', 'RW': False})
    with pytest.raises(ValueError):
        m.verify_boundary(actual)
