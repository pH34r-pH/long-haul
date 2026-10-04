"""Opt-in real CPU smoke, adapted from #197 to the merged request contract.

Only public text is used. Reference token IDs qualify the inspected SmolLM2
conversion on these probes, not arbitrary prompts, task quality or Fleet admission.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from long_haul.adapters.llama_cpp import LlamaCppAdapter
from long_haul.models import InferenceProfile, ModelArtifact
from long_haul.runtime import ExecutionRequest

PIN = json.loads((Path(__file__).parent / 'fixtures/native-completion.json').read_text())


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def record(name, payload):
    directory = os.environ.get('LONG_HAUL_NATIVE_EVIDENCE')
    if directory:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        (path / f'{name}.json').write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'scope': PIN['scope'], 'check': name, **payload}, sort_keys=True))


@pytest.fixture(scope='module')
def native_assets():
    names = ('LONG_HAUL_NATIVE_COMPLETION', 'LONG_HAUL_NATIVE_GGUF',
             'LONG_HAUL_NATIVE_TOKENIZE', 'LONG_HAUL_REFERENCE_TOKENIZER')
    values = [os.environ.get(name) for name in names]
    if not all(values):
        if os.environ.get('LONG_HAUL_REQUIRE_NATIVE_SMOKE') == '1':
            pytest.fail('required native smoke inputs missing; no skipped qualification')
        pytest.skip('explicit native assets not supplied')
    binary, model, tokenize, reference = map(Path, values)
    assert binary.is_file() and model.is_file() and tokenize.is_file()
    assert file_digest(model) == PIN['model_sha256']
    for filename, expected in PIN['tokenizer_files'].items():
        assert file_digest(reference / filename) == expected
    record('asset-identity', {'model_sha256': file_digest(model),
                              'completion_sha256': file_digest(binary),
                              'tokenize_sha256': file_digest(tokenize),
                              'upstream_commit': PIN['upstream_commit']})
    return binary, model, tokenize, reference


@pytest.fixture
def native_request(native_assets):
    binary, model, _, _ = native_assets
    profile = InferenceProfile(
        id='public-cpu-completion-smoke', runtime_id='llama.cpp', strategy='resident',
        artifact=ModelArtifact(foundation=PIN['model_repository'],
                              revision=PIN['model_revision'], quantization='Q4_K_M'),
        participating_resources=['hosted-ci-cpu'],
        options={'model_path': str(model), 'context_size': 512, 'threads': 1,
                 'threads_batch': 1, 'gpu_layers': 0, 'no_warmup': True},
    )
    request = ExecutionRequest(
        request_id='native-smoke', profile=profile, prompt=PIN['prepared_prompt'],
        prompt_mode='rendered', context_tokens=512, seed=42,
        max_tokens=16, temperature=0.7, timeout_seconds=60,
    )
    return LlamaCppAdapter(binary), request


def native_tokens(native_assets, prompt):
    _, model, tokenize, _ = native_assets
    command = [str(tokenize), '-m', str(model), '-p', prompt, '--ids',
               '--no-bos', '--no-escape', '--ctx-size', '512', '-ngl', '0']
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('LLAMA_ARG_')}
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding='utf-8', timeout=60, check=True, env=env)
    tokens = json.loads(result.stdout)
    assert isinstance(tokens, list) and tokens and all(type(x) is int for x in tokens)
    return tokens


def test_real_completion_matches_explicit_upstream_invocation(native_request, native_assets):
    adapter, request = native_request
    # Independent argv; never use adapter._command as its own correctness oracle.
    command = [str(adapter.binary), '-m', request.profile.options['model_path'],
               '-p', request.prompt, '-n', '16', '--temp', '0.7', '--seed', '42',
               '--ctx-size', '512', '--threads', '1', '--threads-batch', '1',
               '-ngl', '0', '--no-warmup', '--no-conversation', '--no-display-prompt',
               '--no-escape', '--no-context-shift', '--fit', 'off']
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('LLAMA_ARG_')}
    direct = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding='utf-8', timeout=60, check=True, env=env)
    result = adapter.execute(request)
    record('native-reference-parity', {'direct_stdout': direct.stdout,
                                       'direct_stderr': direct.stderr,
                                       'result': result.model_dump(mode='json')})
    assert result.success, result.model_dump_json()
    assert result.output and result.output.strip()
    assert request.prompt not in result.output
    assert result.output == direct.stdout
    count = re.search(r'prompt eval time[^\n]*?/\s*(\d+)\s+tokens?', direct.stderr)
    assert count is not None, direct.stderr
    assert result.timings.prompt_tokens == int(count.group(1))
    assert result.timings.prompt_tokens == len(native_tokens(native_assets, request.prompt))
    # Native eval 'runs' is not assumed equal to emitted completion-token count.


def test_real_completion_restarts_with_same_seed_and_no_prompt_state(native_request):
    adapter, request = native_request
    first = adapter.execute(request)
    second = adapter.execute(request.model_copy(update={'request_id': 'native-repeat'}))
    record('same-build-seeded-repeat', {'first': first.model_dump(mode='json'),
                                        'second': second.model_dump(mode='json')})
    assert first.success and second.success
    assert first.output and first.output.strip()
    assert first.output == second.output
    assert first.runtime.fingerprint == second.runtime.fingerprint


@pytest.mark.parametrize('case,text', [
    ('greeting', 'Say hello in a short sentence.'),
    ('unicode', 'Read café and résumé.'),
    ('escapes', 'Keep literal \\n separate from this newline:\nnext line.'),
    ('whitespace', '  Preserve the two leading spaces and trailing newline.\n'),
])
def test_native_token_ids_match_pinned_reference(native_assets, case, text):
    # Optional packages are required (not importorskip) once native inputs exist.
    from jinja2 import StrictUndefined
    from jinja2.sandbox import ImmutableSandboxedEnvironment
    from tokenizers import Tokenizer

    reference = native_assets[3]
    config = json.loads((reference / 'tokenizer_config.json').read_text())
    template = config['chat_template']
    assert hashlib.sha256(template.encode()).hexdigest() == PIN['chat_template_sha256']
    environment = ImmutableSandboxedEnvironment(
        trim_blocks=True, lstrip_blocks=True, undefined=StrictUndefined)
    messages = [{'role': 'system', 'content': 'You are a helpful assistant.'},
                {'role': 'user', 'content': text}]
    rendered = environment.from_string(template).render(
        messages=messages, add_generation_prompt=True)
    if case == 'greeting':
        assert rendered == PIN['prepared_prompt']
    expected = Tokenizer.from_file(str(reference / 'tokenizer.json')).encode(
        rendered, add_special_tokens=False).ids
    observed = native_tokens(native_assets, rendered)
    record(f'token-parity-{case}', {'rendered_prompt': rendered,
                                   'reference_ids': expected, 'native_ids': observed})
    assert observed == expected
    assert len(observed) + 16 <= 512
