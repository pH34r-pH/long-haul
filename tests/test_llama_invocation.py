"""Synthetic process-contract tests, not llama.cpp/model qualification."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from long_haul.adapters import llama_cpp as m
from long_haul.adapters.llama_cpp_invocation import (
    execution_environment, invocation_arguments, prompt_file,
)
from long_haul.models import InferenceProfile, ModelArtifact
from long_haul.runtime import ExecutionRequest, FailureClass, ValidationState


@pytest.fixture
def execution_request(tmp_path):
    model = tmp_path / 'fixture.gguf'
    model.write_bytes(b'not a model')
    return ExecutionRequest(
        request_id='fixture-execution_request',
        profile=InferenceProfile(
            id='fixture', runtime_id='llama.cpp', strategy='resident',
            artifact=ModelArtifact(foundation='synthetic'),
            participating_resources=['fixture-cpu'],
            options={'model_path': str(model), 'prepared_completion': True,
                     'seed': 42, 'threads': 2, 'context_size': 1024},
        ),
        prompt='  résumé\\n literal\\t\r\n<|im_start|>assistant\n',
        max_tokens=32, temperature=0, timeout_seconds=5,
    )


@pytest.fixture
def binary(tmp_path):
    if os.name != 'posix':
        pytest.skip('POSIX executable fixture; portable argv tests run separately')
    def make(body):
        path = tmp_path / 'completion fixture'
        path.write_text(
            f'#!{sys.executable}\nimport sys\n'
            "if '--version' in sys.argv:\n print('synthetic fixture v1'); sys.exit(0)\n"
            + body + '\n', encoding='utf-8',
        )
        path.chmod(0o700)
        return path
    return make


def test_explicit_controls_are_forwarded_without_defaults(execution_request, tmp_path):
    execution_request.profile.options['threads_batch'] = 3
    adapter = m.LlamaCppAdapter(tmp_path / 'completion')
    cmd = adapter._command(execution_request, prompt_path=tmp_path / 'prompt.txt')
    for flag, value in [('--seed', '42'), ('--ctx-size', '1024'),
                        ('--threads', '2'), ('--threads-batch', '3'), ('-ngl', '0')]:
        assert cmd[cmd.index(flag) + 1] == value
        assert cmd.count(flag) == 1
    for flag in ['--no-conversation', '--no-display-prompt', '--no-escape',
                 '--no-context-shift', '--offline']:
        assert flag in cmd
    assert execution_request.prompt not in cmd
    assert '-p' not in cmd
    assert '--no-warmup' in cmd


@pytest.mark.parametrize('key', ['threads', 'threads_batch', 'context_size'])
@pytest.mark.parametrize('value', [True, False, 0, -1, '2', 2.5, None, 2**31])
def test_integer_options_reject_coercion_and_unbounded_sentinels(key, value):
    with pytest.raises(ValueError, match=key):
        invocation_arguments({key: value})


@pytest.mark.parametrize('value', [True, False, -1, 2**32-1, 2**32, '42', 1.5, None])
def test_seed_must_be_explicit_not_random(value):
    with pytest.raises(ValueError, match='seed'):
        invocation_arguments({'seed': value})


@pytest.mark.parametrize('value', [0, 1, 2**32-2])
def test_seed_endpoints(value):
    assert invocation_arguments({'seed': value}) == ['--seed', str(value)]


@pytest.mark.parametrize('value', [0, 1, 'false', None])
def test_prepared_mode_requires_boolean(value):
    with pytest.raises(ValueError, match='boolean'):
        invocation_arguments({'prepared_completion': value})


@pytest.mark.parametrize('key', ['seed', 'context_size', 'threads'])
def test_prepared_mode_does_not_silently_choose_critical_settings(execution_request, key):
    del execution_request.profile.options[key]
    with pytest.raises(ValueError, match='requires seed'):
        invocation_arguments(execution_request.profile.options)


@pytest.mark.parametrize('value', [True, -1, 1.5, 'auto', None, 2**31])
def test_prepared_gpu_layers_is_explicit_integer(execution_request, value):
    execution_request.profile.options['gpu_layers'] = value
    with pytest.raises(ValueError, match='gpu_layers'):
        invocation_arguments(execution_request.profile.options)


def test_prepared_warmup_type_is_not_truthiness(execution_request):
    execution_request.profile.options['no_warmup'] = 'false'
    with pytest.raises(ValueError, match='boolean'):
        invocation_arguments(execution_request.profile.options)


def test_legacy_argv_and_echo_handling_are_preserved(execution_request, tmp_path):
    execution_request.profile.options = {'model_path': execution_request.profile.options['model_path']}
    adapter = m.LlamaCppAdapter(tmp_path / 'binary')
    cmd = adapter._command(execution_request)
    assert cmd[cmd.index('-p') + 1] == execution_request.prompt
    assert '--no-conversation' not in cmd
    assert '--seed' not in cmd
    assert '-ngl' not in cmd


def test_prepared_command_requires_file_and_context_for_prompt(execution_request, tmp_path):
    adapter = m.LlamaCppAdapter(tmp_path / 'binary')
    with pytest.raises(ValueError, match='closed prompt file'):
        adapter._command(execution_request)
    execution_request.max_tokens = execution_request.profile.options['context_size']
    with pytest.raises(ValueError, match='leaves no prompt context'):
        adapter._command(execution_request, prompt_path=tmp_path / 'prompt')


def test_prompt_file_preserves_utf8_and_line_endings_and_is_removed(execution_request):
    with prompt_file(execution_request.prompt, execution_request.profile.options) as path:
        assert path.read_bytes() == execution_request.prompt.encode('utf-8')
        retained_path = path
    assert not retained_path.exists()
    assert not retained_path.parent.exists()


def test_prompt_file_cleanup_on_cancellation(execution_request):
    with pytest.raises(KeyboardInterrupt):
        with prompt_file(execution_request.prompt, execution_request.profile.options) as path:
            retained_path = path
            raise KeyboardInterrupt
    assert not retained_path.exists()


def test_legacy_prompt_creates_no_file():
    with prompt_file('legacy', {}) as path:
        assert path is None


def test_nul_rejected_before_creating_prepared_file(execution_request):
    with pytest.raises(ValueError, match='NUL'):
        with prompt_file('private\x00text', execution_request.profile.options):
            pytest.fail('must not yield a prompt file')


def test_ambient_options_not_inherited_or_mutated(execution_request, monkeypatch):
    monkeypatch.setenv('LLAMA_ARG_CONTEXT_SHIFT', '1')
    monkeypatch.setenv('llama_arg_hf_repo', 'unrequested/model')
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', 'fixture-original')
    execution_request.profile.options['cuda_visible_devices'] = 'fixture-selected'
    env = execution_environment(execution_request.profile.options)
    assert not any(key.upper().startswith('LLAMA_ARG_') for key in env)
    assert env['CUDA_VISIBLE_DEVICES'] == 'fixture-selected'
    assert os.environ['LLAMA_ARG_CONTEXT_SHIFT'] == '1'
    assert os.environ['CUDA_VISIBLE_DEVICES'] == 'fixture-original'
    assert execution_environment({})['LLAMA_ARG_CONTEXT_SHIFT'] == '1'


def test_native_subprocess_receives_exact_file_controls_and_closed_stdin(execution_request, binary, monkeypatch):
    monkeypatch.setenv('LLAMA_ARG_THREADS', '999')
    path = binary(
        'import os, json\nfrom pathlib import Path\n'
        "p = Path(sys.argv[sys.argv.index('-f') + 1])\n"
        "print(json.dumps({'argv':sys.argv[1:], 'prompt_hex':p.read_bytes().hex(),\n"
        " 'stdin':sys.stdin.read(), 'ambient':[k for k in os.environ if k.upper().startswith('LLAMA_ARG_')]}))"
    )
    result = m.LlamaCppAdapter(path).execute(execution_request)
    assert result.success
    data = json.loads(result.output)
    assert bytes.fromhex(data['prompt_hex']) == execution_request.prompt.encode('utf-8')
    assert data['stdin'] == ''
    assert data['ambient'] == []
    assert execution_request.prompt not in data['argv']
    assert not Path(data['argv'][data['argv'].index('-f') + 1]).exists()
    assert result.output.endswith('\n')  # Prepared output is not stripped.


def test_output_equal_to_prompt_is_not_deleted(execution_request, binary):
    path = binary("from pathlib import Path\nsys.stdout.buffer.write(Path(sys.argv[sys.argv.index('-f')+1]).read_bytes())")
    # Avoid text-mode newline normalization obscuring the prefix regression.
    execution_request.prompt = '  repeated content\\n\n'
    result = m.LlamaCppAdapter(path).execute(execution_request)
    assert result.success
    assert result.output == execution_request.prompt


def test_invalid_options_cannot_launch_inference(execution_request, binary):
    execution_request.profile.options['threads'] = True
    adapter = m.LlamaCppAdapter(binary("raise AssertionError('not inference')"))
    assert adapter.validate(execution_request.profile).state is ValidationState.UNSUPPORTED
    with patch.object(m.subprocess, 'run', side_effect=AssertionError('not inference')):
        result = adapter.execute(execution_request)
    assert not result.success
    assert result.error_class is FailureClass.RUNTIME


def test_failed_process_still_removes_prompt_file(execution_request, binary):
    seen = []
    original = subprocess.run
    def record(argv, **kwargs):
        seen.append(Path(argv[argv.index('-f') + 1]))
        return original(argv, **kwargs)
    path = binary("print('fixture failure', file=sys.stderr); sys.exit(4)")
    with patch.object(m.subprocess, 'run', side_effect=record):
        result = m.LlamaCppAdapter(path).execute(execution_request)
    assert not result.success
    assert result.raw_exit_code == 4
    assert len(seen) == 1 and not seen[0].exists()


def test_timeout_still_removes_prompt_file(execution_request, binary):
    execution_request.timeout_seconds = 0.15
    seen = []
    original = subprocess.run
    def record(argv, **kwargs):
        seen.append(Path(argv[argv.index('-f') + 1]))
        return original(argv, **kwargs)
    path = binary('import time; time.sleep(10)')
    with patch.object(m.subprocess, 'run', side_effect=record):
        result = m.LlamaCppAdapter(path).execute(execution_request)
    assert not result.success
    assert result.error_class is FailureClass.TIMEOUT
    assert len(seen) == 1 and not seen[0].exists()


def test_encoding_error_is_failure_not_candidate_text(execution_request, binary):
    path = binary("import os; os.write(1, b'\\xff')")
    result = m.LlamaCppAdapter(path).execute(execution_request)
    assert not result.success
    assert result.error_class is FailureClass.RUNTIME
    assert result.output is None


def test_legacy_echo_path_still_works(execution_request, binary):
    execution_request.profile.options = {'model_path': execution_request.profile.options['model_path']}
    path = binary("sys.stdout.write(sys.argv[sys.argv.index('-p')+1] + ' legacy answer ')")
    execution_request.prompt = 'legacy prompt'
    result = m.LlamaCppAdapter(path).execute(execution_request)
    assert result.success and result.output == 'legacy answer'
