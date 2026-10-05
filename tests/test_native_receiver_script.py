"""Exercise shell orchestration with fake Docker/sudo; no container is qualified."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/qualify_native_receiver.sh'
PIN = ROOT / 'tests/fixtures/native-receiver.json'
FAKE = '''import json, os, pathlib, sys
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
with (root/'commands.jsonl').open('a') as stream:
    stream.write(json.dumps([name, *args])+'\\n')
if name == 'python':
    if args[:3] == ['-m', 'pip', 'download']:
        if os.environ.get('FAIL_AT') == 'download': sys.exit(8)
        wheels = pathlib.Path(args[args.index('--dest')+1])
        (wheels/'synthetic.whl').write_bytes(b'not a real wheel; never installed')
    else: os.execv(os.environ['REAL_PYTHON'], [os.environ['REAL_PYTHON'], '-S', *args])
elif name == 'sudo':
    if args[1] not in ('mount','chown','umount'): sys.exit(99)
    if os.environ.get('FAIL_AT') == args[1]: sys.exit(9)
elif name == 'id': print(1001)
elif name == 'docker':
    action = args[0]
    if os.environ.get('FAIL_AT') == action: sys.exit(11)
    if action == 'create': print('a'*64)
    elif action == 'start':
        (root/'native-receiver/output/fixture-observation.json').write_text('{}\\n')
        if os.environ.get('FAIL_AT') == 'timeout': sys.exit(124)
        print('fixture process finished; not model output')
    elif action in ('image', 'inspect'): print('[]')
    elif action == 'rm': print('a'*64)
    elif action == 'logs': print('synthetic engine log')
    elif action != 'pull': sys.exit(98)
else: sys.exit(97)
'''


@pytest.fixture
def sandbox(tmp_path):
    work, temp, fake = [tmp_path / name for name in ('work', 'temporary', 'fake-bin')]
    for path in (work / 'tests/fixtures', temp, fake):
        path.mkdir(parents=True)
    pin = json.loads(PIN.read_text())
    (work / 'tests/fixtures/native-receiver.json').write_text(json.dumps(pin))
    model = b'synthetic model bytes; not used for inference'
    native = {'model_sha256': hashlib.sha256(model).hexdigest()}
    encoded = json.dumps(native).encode()
    (work / pin['native_input_pin']).write_bytes(encoded)
    assets = temp / 'native-assets'
    (assets / 'bin').mkdir(parents=True)
    payload = {'model.gguf': model, 'inputs.json': encoded,
               'long-haul-source.txt': ('b'*40+'\n').encode(),
               'bin/llama-server': b'not executed'}
    for name, data in payload.items():
        (assets / name).write_bytes(data)
    (assets / 'SHA256SUMS').write_text(''.join(
        hashlib.sha256(data).hexdigest()+'  ./'+name+'\n' for name, data in payload.items()))
    for name in ('python', 'docker', 'sudo', 'id'):
        path = fake / name
        path.write_text(f'#!{sys.executable} -S\n'+FAKE)
        path.chmod(0o700)
    env = {**os.environ, 'PATH': str(fake)+os.pathsep+os.environ['PATH'],
           'RUNNER_TEMP': str(temp), 'FIXTURE_ROOT': str(temp), 'REAL_PYTHON': sys.executable,
           'GITHUB_REPOSITORY': 'synthetic/repo', 'GITHUB_SHA': 'b'*40,
           'GITHUB_RUN_ID': '1', 'GITHUB_RUN_ATTEMPT': '1',
           'GITHUB_TOKEN': 'synthetic-must-not-enter-container'}
    return work, temp, env


def run(sandbox, failure=''):
    work, temp, env = sandbox
    result = subprocess.run(['bash', str(SCRIPT)], cwd=work,
                            env={**env, 'FAIL_AT': failure},
                            text=True, capture_output=True, timeout=15)
    calls = [json.loads(line) for line in (temp/'commands.jsonl').read_text().splitlines()]
    return result, calls, temp/'native-receiver/evidence'


def docker_calls(calls, operation):
    return [call for call in calls if call[:2] == ['docker', operation]]


def test_shell_prepares_exact_assets_and_bounded_offline_container(sandbox):
    result, calls, evidence = run(sandbox)
    assert result.returncode == 0, result.stderr
    argv, = docker_calls(calls, 'create')
    for flag, value in (('--platform', 'linux/amd64'), ('--network', 'none'),
                        ('--user', '1001:1001'), ('--cpus', '1'), ('--memory', '1g'),
                        ('--memory-swap', '1g'), ('--pids-limit', '64'),
                        ('--cap-drop', 'ALL'), ('--security-opt', 'no-new-privileges')):
        assert argv[argv.index(flag)+1] == value
    assert '--read-only' in argv and '--init' in argv
    assert json.loads(PIN.read_text())['image'] in argv
    assert 'synthetic-must-not-enter-container' not in json.dumps(argv)
    assert 'GITHUB_TOKEN' not in json.dumps(argv)
    assert '--no-index --no-deps' in argv[-1]
    assert '/wheels/*.whl' in argv[-1]
    assert docker_calls(calls, 'rm') == [['docker', 'rm', '--force', 'a'*64]]
    assert (evidence/'fixture-observation.json').exists()
    assert (evidence/'wheels.sha256').exists()
    assert (evidence/'source.json').exists()


@pytest.mark.parametrize('failure', ['download', 'pull'])
def test_prelaunch_failure_never_creates_or_removes_container(sandbox, failure):
    result, calls, evidence = run(sandbox, failure)
    assert result.returncode != 0
    assert not docker_calls(calls, 'create') and not docker_calls(calls, 'rm')
    assert (evidence/'source.json').exists()
    assert not [c for c in calls if c[:2] == ['sudo', '-n']]


def test_create_failure_unmounts_but_does_not_remove_other_container(sandbox):
    result, calls, _ = run(sandbox, 'create')
    assert result.returncode != 0
    assert not docker_calls(calls, 'rm')
    assert any(c[:3] == ['sudo', '-n', 'umount'] for c in calls)


@pytest.mark.parametrize('failure,code', [('start', 11), ('timeout', 124)])
def test_failed_run_retains_diagnostics_and_removes_only_owned_container(sandbox, failure, code):
    result, calls, evidence = run(sandbox, failure)
    assert result.returncode == code
    assert docker_calls(calls, 'rm') == [['docker', 'rm', '--force', 'a'*64]]
    assert (evidence/'container-inspect.json').exists()
    assert (evidence/'container.log').exists()
    assert any(c[:3] == ['sudo', '-n', 'umount'] for c in calls)


@pytest.mark.parametrize('failure', ['rm', 'umount'])
def test_cleanup_failure_cannot_appear_successful(sandbox, failure):
    result, _, evidence = run(sandbox, failure)
    assert result.returncode != 0
    assert (evidence/'fixture-observation.json').exists()


@pytest.mark.parametrize('target', ['model.gguf', 'inputs.json', 'long-haul-source.txt'])
def test_mutated_assets_fail_before_dependency_or_image_acquisition(sandbox, target):
    _, temp, _ = sandbox
    (temp/'native-assets'/target).write_bytes(b'changed')
    result, calls, _ = run(sandbox)
    assert result.returncode != 0
    assert not any(c[0] == 'docker' for c in calls)
    assert not any(c[:4] == ['python','-m','pip','download'] for c in calls)


def test_runtime_mounts_are_readonly_and_no_host_endpoint_is_exposed(sandbox):
    result, calls, _ = run(sandbox)
    assert result.returncode == 0
    argv, = docker_calls(calls, 'create')
    mounts = [argv[i+1] for i, value in enumerate(argv) if value == '--mount']
    for destination in ('/assets', '/wheels', '/source'):
        matching, = [value for value in mounts if f'dst={destination},' in value]
        assert matching.endswith(',readonly')
    assert '--privileged' not in argv and '--publish' not in argv
    assert not any('/var/run/docker.sock' in value for value in mounts)
    assert '/tmp:rw,nosuid,nodev,size=512m' in argv


def test_limits_match_the_checked_in_qualification_profile():
    assert json.loads(PIN.read_text())['limits'] == {
        'cores': 1, 'ramMiB': 1024, 'tmpdirMiB': 512, 'outdirMiB': 64,
        'pids': 64, 'wallSeconds': 300,
    }
