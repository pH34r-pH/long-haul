"""Real, opt-in loopback server qualification using the existing public tiny model."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import time

import pytest

from long_haul.adapters.llama_cpp import LlamaCppAdapter
from long_haul.adapters.llama_server import LlamaServerAdapter
from long_haul.models import InferenceProfile, ModelArtifact
from long_haul.runtime import ExecutionRequest, ProfileValidation, ValidationState
from test_llama_native_completion import PIN, file_digest, native_tokens, record

pytest_plugins = ('test_llama_native_completion',)


def wait_for_server(process, port):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail('owned server exited before readiness; inspect retained server.log')
        connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
        try:
            connection.request('GET', '/health')
            with connection.getresponse() as response:
                if response.status == 200 and json.loads(response.read()) == {'status': 'ok'}:
                    return
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(.1)
    pytest.fail('owned server did not become ready within the startup bound')


@pytest.fixture(scope='module')
def owned_server(native_assets, tmp_path_factory):
    value = os.environ.get('LONG_HAUL_NATIVE_SERVER')
    if not value:
        if os.environ.get('LONG_HAUL_REQUIRE_NATIVE_SMOKE') == '1':
            pytest.fail('required native server missing')
        pytest.skip('explicit native server not supplied')
    binary = Path(value)
    assert binary.is_file()
    model = native_assets[1]
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    root = Path(os.environ.get('LONG_HAUL_NATIVE_EVIDENCE') or tmp_path_factory.mktemp('server'))
    root.mkdir(parents=True, exist_ok=True)
    command = [str(binary), '-m', str(model), '--host', '127.0.0.1', '--port', str(port),
               '--parallel', '1', '--ctx-size', '512', '--threads', '1', '--threads-batch', '1',
               '-ngl', '0', '--fit', 'off', '--no-context-shift', '--no-warmup', '--no-webui']
    env = {key: val for key, val in os.environ.items() if not key.upper().startswith('LLAMA_ARG_')}
    started = time.monotonic()
    with (root / 'server.log').open('wb') as log:
        # Inherit the outer timeout process group; hard job expiry must stop both.
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   env=env)
        try:
            wait_for_server(process, port)
            runtime = LlamaCppAdapter(binary).identity()
            assert runtime.capabilities.get('identity_probe') == 'ready'
            record('server-identity', {'sha256': file_digest(binary), 'argv': command,
                                       'startup_seconds': time.monotonic() - started,
                                       'runtime': runtime.model_dump(mode='json'),
                                       'scope': 'owned hosted CPU smoke only'})
            yield port, model, runtime
        finally:
            try:
                process.terminate()
                process.wait(timeout=5)
            except ProcessLookupError:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            record('server-cleanup', {'returncode': process.returncode,
                                      'process_reaped': process.poll() is not None})


def server_request(owned_server, *, schema=None):
    port, model, runtime = owned_server
    options = dict(model_path=str(model), transport='llama-server', server_port=port,
                   context_size=512, threads=1, threads_batch=1, gpu_layers=0)
    if schema is not None:
        options['json_schema'] = schema
    profile = InferenceProfile(
        id='owned-server-smoke', runtime_id='llama.cpp', strategy='resident',
        artifact=ModelArtifact(foundation=PIN['model_repository'], revision=PIN['model_revision'],
                              quantization='Q4_K_M'), participating_resources=['hosted-ci-cpu'],
        options=options,
    )
    binding = ProfileValidation(runtime=runtime, profile_id=profile.id, artifact_key=profile.artifact.key,
                                resources=profile.participating_resources, strategy=profile.strategy,
                                options=options, state=ValidationState.UNKNOWN,
                                rationale='bounded owned smoke candidate; not fleet qualification')
    replies = []
    adapter = LlamaServerAdapter(port, binding, observe=lambda path, obj: replies.append((path, obj)))
    request = ExecutionRequest(request_id='server-smoke', profile=profile, prompt=PIN['prepared_prompt'],
                               prompt_mode='rendered', context_tokens=512, seed=42, temperature=.7,
                               max_tokens=32, timeout_seconds=60, allow_unknown_runtime=True)
    return adapter, request, replies


@pytest.mark.parametrize('text', ['Say hello in a short sentence.', 'Read café. Literal \\n versus\nnewline.'])
def test_server_template_and_tokens_match_native_cli(owned_server, native_assets, text):
    adapter, _, _ = server_request(owned_server)
    messages = [{'role': 'system', 'content': 'You are a helpful assistant.'},
                {'role': 'user', 'content': text}]
    prompt = adapter.apply_template(messages, deadline=time.monotonic() + 30)
    if text.startswith('Say hello'):
        assert prompt == PIN['prepared_prompt']
    ids = adapter.tokenize(prompt, deadline=time.monotonic() + 30)
    expected = native_tokens(native_assets, prompt)
    record('server-token-parity-' + ('greeting' if text.startswith('Say') else 'unicode'),
           {'prompt': prompt, 'server_ids': ids, 'native_ids': expected})
    assert ids == expected


def test_server_content_accounting_and_cache_disabled_repetition(owned_server):
    adapter, request, replies = server_request(owned_server)
    first = adapter.execute(request)
    second = adapter.execute(request.model_copy(update={'request_id': 'second'}))
    record('server-completion-repeat', {'first': first.model_dump(mode='json'),
                                       'second': second.model_dump(mode='json'), 'upstream': replies})
    assert first.success and second.success
    assert first.output and '[end of text]' not in first.output
    assert first.output == second.output
    finals = [reply for path, reply in replies if path == '/completion']
    assert len(finals) == 2 and finals[0]['tokens'] == finals[1]['tokens']
    for result, raw in zip((first, second), finals, strict=True):
        assert result.output == raw['content']
        assert result.timings.prompt_tokens == raw['tokens_evaluated']
        assert result.timings.generated_tokens == raw['tokens_predicted']
        assert len(raw['tokens']) <= raw['tokens_predicted'] <= 32
        assert raw['truncated'] is False
    assert adapter.validate(request.profile).state is ValidationState.UNKNOWN


def test_server_json_is_parseable_without_stripping_native_display_text(owned_server):
    schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
              'required': ['ok'], 'additionalProperties': False}
    adapter, request, replies = server_request(owned_server, schema=schema)
    request.prompt = adapter.apply_template(
        [{'role': 'system', 'content': 'Return only JSON.'},
         {'role': 'user', 'content': 'Return an object with ok set to true.'}],
        deadline=time.monotonic() + 30)
    request.temperature, request.max_tokens = 0, 64
    result = adapter.execute(request)
    record('server-structured-content', {'result': result.model_dump(mode='json'), 'upstream': replies})
    assert result.success
    parsed = json.loads(result.output)
    assert set(parsed) == {'ok'} and type(parsed['ok']) is bool
    # Grammar validity is not semantic/task correctness; do not score the bool.


def test_server_rejects_real_context_overflow_before_generation(owned_server):
    adapter, request, replies = server_request(owned_server)
    request.prompt = 'token ' * 600
    result = adapter.execute(request)
    record('server-context-refusal', {'result': result.model_dump(mode='json'),
                                     'endpoints': [path for path, _ in replies]})
    assert not result.success
    assert [path for path, _ in replies] == ['/tokenize']
