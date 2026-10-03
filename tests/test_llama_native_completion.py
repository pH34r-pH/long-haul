"""Opt-in real CPU completion smoke; never DBD quality or hardware acceptance."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from long_haul.adapters.llama_cpp import LlamaCppAdapter
from long_haul.models import InferenceProfile, ModelArtifact
from long_haul.runtime import ExecutionRequest

PIN = json.loads((Path(__file__).parent / 'fixtures/native-completion.json').read_text())


@pytest.fixture
def native_request():
    binary = os.environ.get('LONG_HAUL_NATIVE_COMPLETION')
    model = os.environ.get('LONG_HAUL_NATIVE_GGUF')
    if not binary or not model:
        pytest.skip('explicit native completion binary and verified GGUF not supplied')
    assert Path(binary).is_file() and Path(model).is_file()
    with Path(model).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    assert digest == PIN['model_sha256']
    profile = InferenceProfile(
        id='public-cpu-completion-smoke', runtime_id='llama.cpp', strategy='resident',
        artifact=ModelArtifact(foundation=PIN['model_repository'],
                               revision=PIN['model_revision'], quantization='Q4_K_M'),
        participating_resources=['hosted-ci-cpu'],
        options={'model_path': model, 'prepared_completion': True,
                 'context_size': 512, 'threads': 1, 'threads_batch': 1,
                 'gpu_layers': 0, 'seed': 42, 'no_warmup': True},
    )
    return LlamaCppAdapter(binary), ExecutionRequest(
        request_id='native-smoke', profile=profile, prompt=PIN['prepared_prompt'],
        max_tokens=16, temperature=0.7, timeout_seconds=60,
    )


def test_real_completion_matches_explicit_upstream_invocation(native_request, tmp_path):
    adapter, req = native_request
    rendered = tmp_path / 'reference-prompt.txt'
    rendered.write_bytes(req.prompt.encode('utf-8'))
    # Independently construct the reference argv, rather than calling _command.
    command = [str(adapter.binary), '-m', req.profile.options['model_path'],
               '-f', str(rendered), '-n', '16', '--temp', '0.7', '--seed', '42',
               '--ctx-size', '512', '--threads', '1', '--threads-batch', '1',
               '-ngl', '0', '--no-warmup', '--no-conversation', '--no-display-prompt',
               '--no-escape', '--no-context-shift', '--offline']
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('LLAMA_ARG_')}
    direct = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding='utf-8', timeout=60, check=True, env=env)
    result = adapter.execute(req)
    assert result.success, result.model_dump_json()
    assert result.output and result.output.strip()
    assert req.prompt not in result.output
    assert result.output == direct.stdout
    print(json.dumps({'scope': PIN['scope'], 'comparison': 'native-reference-parity',
                      'source': PIN['upstream_commit'], 'model_sha256': PIN['model_sha256'],
                      'result': result.model_dump(mode='json')}, sort_keys=True))


def test_real_completion_restarts_with_same_seed_and_no_prompt_state(native_request):
    adapter, req = native_request
    first = adapter.execute(req)
    second = adapter.execute(req.model_copy(update={'request_id': 'native-repeat'}))
    assert first.success and second.success
    assert first.output == second.output
    assert first.runtime.fingerprint == second.runtime.fingerprint
    print(json.dumps({'scope': PIN['scope'], 'comparison': 'same-build-seeded-repeat',
                      'first': first.model_dump(mode='json'),
                      'second': second.model_dump(mode='json')}, sort_keys=True))
