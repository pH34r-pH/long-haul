"""Native process-boundary fixtures, not model or physical-vessel qualification."""
import json
import os
import sys
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from long_haul.adapters import llama_cpp as m
from long_haul.models import InferenceProfile, ModelArtifact
from long_haul.runtime import ExecutionRequest, FailureClass


def request(tmp_path, *, options=None, **kwargs):
    model = tmp_path / 'fixture model.gguf'
    model.write_bytes(b'synthetic fixture; not model weights')
    profile = InferenceProfile(
        id='fixture', runtime_id='llama.cpp', strategy='resident',
        artifact=ModelArtifact(foundation='synthetic'),
        participating_resources=['fixture-cpu'],
        options={'model_path': str(model), 'threads': 2, 'context_size': 256,
                 **(options or {})},
    )
    return ExecutionRequest(request_id='fixture-request', profile=profile,
                            prompt='a literal \\n and café', max_tokens=16,
                            prompt_mode='rendered', context_tokens=256,
                            **kwargs)


def executable(tmp_path, body):
    path = tmp_path / 'fixture completion'
    path.write_text(f'#!{sys.executable}\nimport sys\n'
                    "if '--version' in sys.argv:\n"
                    "    print('synthetic-completion v1'); sys.exit(0)\n" + body,
                    encoding='utf-8')
    path.chmod(0o700)
    return path


def command(tmp_path, **kwargs):
    return m.LlamaCppAdapter(tmp_path / 'fixture completion')._command(request(tmp_path, **kwargs))


def test_explicit_zero_seed_threads_context_and_cpu_placement(tmp_path):
    argv = command(tmp_path, seed=0)
    for flag, value in [('--seed', '0'), ('--threads', '2'),
                        ('--threads-batch', '2'), ('--ctx-size', '256'),
                        ('-ngl', '0'), ('--fit', 'off')]:
        assert argv.count(flag) == 1
        assert argv[argv.index(flag) + 1] == value
    for flag in ['--no-conversation', '--no-display-prompt', '--no-escape',
                 '--no-context-shift']:
        assert argv.count(flag) == 1


def test_prompt_is_one_unmodified_argument(tmp_path):
    req = request(tmp_path)
    req.prompt = '  <s>café\\n"; $(touch NOT_EXECUTED)\n</s>  '
    argv = m.LlamaCppAdapter(tmp_path / 'binary')._command(req)
    assert argv[argv.index('-p') + 1] == req.prompt
    assert len([item for item in argv if 'NOT_EXECUTED' in item]) == 1


def test_batch_threads_can_be_separately_pinned(tmp_path):
    argv = command(tmp_path, options={'threads_batch': 3})
    assert argv[argv.index('--threads-batch') + 1] == '3'


def test_stochastic_prepared_call_requires_seed(tmp_path):
    with pytest.raises(ValueError, match='explicit seed'):
        command(tmp_path, temperature=0.7)
    assert '--seed' in command(tmp_path, temperature=0.7, seed=17)


def test_context_changes_are_profile_changes_not_runtime_overrides(tmp_path):
    req = request(tmp_path)
    original_identity = req.profile.identity
    req.context_tokens = 512
    with pytest.raises(ValueError, match='must match'):
        m.LlamaCppAdapter(tmp_path / 'binary')._command(req)
    req.profile.options['context_size'] = 512
    assert req.profile.identity != original_identity
    argv = m.LlamaCppAdapter(tmp_path / 'binary')._command(req)
    assert argv[argv.index('--ctx-size') + 1] == '512'


@pytest.mark.parametrize('field', ['seed', 'context_tokens'])
@pytest.mark.parametrize('value', [True, False, '10', 1.5, -1, 2**32-1])
def test_request_controls_reject_coercion_and_random_seed_sentinel(tmp_path, field, value):
    data = request(tmp_path).model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate(data)


@pytest.mark.parametrize('field', ['temperature', 'timeout_seconds'])
@pytest.mark.parametrize('value', [float('inf'), float('nan'), float('-inf')])
def test_nonfinite_request_controls_rejected(tmp_path, field, value):
    data = request(tmp_path).model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        ExecutionRequest.model_validate(data)


@pytest.mark.parametrize('name', ['threads', 'threads_batch', 'context_size'])
@pytest.mark.parametrize('value', [None, True, '2', 1.5, 0, -1, 2**31])
def test_profile_controls_are_explicit_positive_integers(tmp_path, name, value):
    with pytest.raises(ValueError, match='positive integer'):
        command(tmp_path, options={name: value})


@pytest.mark.parametrize('missing', ['threads', 'context_size'])
def test_rendered_requests_do_not_use_ambient_resource_defaults(tmp_path, missing):
    req = request(tmp_path)
    del req.profile.options[missing]
    with pytest.raises(ValueError):
        m.LlamaCppAdapter(tmp_path / 'binary')._command(req)


def test_rendered_request_needs_explicit_request_context(tmp_path):
    req = request(tmp_path)
    req.context_tokens = None
    with pytest.raises(ValueError, match='explicit matching context'):
        m.LlamaCppAdapter(tmp_path / 'binary')._command(req)


@pytest.mark.parametrize('layers', [-1, True, 'auto', 1.5])
def test_rendered_gpu_layers_reject_implicit_or_coerced_values(tmp_path, layers):
    with pytest.raises(ValueError, match='gpu_layers'):
        command(tmp_path, options={'gpu_layers': layers})


def test_existing_accelerated_placement_arguments_are_preserved(tmp_path):
    req = request(tmp_path, options={'gpu_layers': 20, 'split_mode': 'layer',
                                    'tensor_split': [3, 1], 'main_gpu': 0})
    req.profile.strategy = 'gpu-offload'
    argv = m.LlamaCppAdapter(tmp_path / 'binary')._command(req)
    for flag, value in [('-ngl', '20'), ('--split-mode', 'layer'),
                        ('--tensor-split', '3,1'), ('--main-gpu', '0')]:
        assert argv[argv.index(flag) + 1] == value


def test_legacy_defaults_remain_legacy(tmp_path):
    req = request(tmp_path)
    req = ExecutionRequest(request_id=req.request_id, profile=req.profile, prompt=req.prompt)
    req.profile.options.pop('context_size')
    req.profile.options.pop('threads')
    argv = m.LlamaCppAdapter(tmp_path / 'binary')._command(req)
    assert req.prompt_mode == 'runtime-default'
    assert not {'--ctx-size', '--seed', '--threads', '--fit', '--no-conversation'} & set(argv)


def test_rendered_environment_discards_only_llama_argument_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv('LLAMA_ARG_CTX_SIZE', '8192')
    monkeypatch.setenv('llama_arg_temperature', '99')
    monkeypatch.setenv('DBD_FIXTURE_KEEP', 'keep')
    req = request(tmp_path, options={'cuda_visible_devices': 'fixture-id'})
    env = m._inference_environment(req)
    assert not any(key.upper().startswith('LLAMA_ARG_') for key in env)
    assert env['DBD_FIXTURE_KEEP'] == 'keep'
    assert env['CUDA_VISIBLE_DEVICES'] == 'fixture-id'
    assert os.environ['LLAMA_ARG_CTX_SIZE'] == '8192'


@pytest.mark.skipif(os.name != 'posix', reason='fixture shebang requires POSIX')
def test_real_fixture_process_observes_exact_argv_closed_stdin_and_unicode(tmp_path, monkeypatch):
    monkeypatch.setenv('LLAMA_ARG_CTX_SIZE', '99999')
    binary = executable(tmp_path,
        "import json, os\n"
        "assert sys.stdin.read() == ''\n"
        "assert not any(k.upper().startswith('LLAMA_ARG_') for k in os.environ)\n"
        "print(json.dumps({'argv': sys.argv[1:]}, ensure_ascii=False))\n")
    req = request(tmp_path, seed=9, temperature=0.5)
    result = m.LlamaCppAdapter(binary).execute(req)
    assert result.success
    argv = json.loads(result.output)['argv']
    assert argv[argv.index('-p') + 1] == req.prompt
    assert argv[argv.index('--seed') + 1] == '9'
    assert result.runtime.capabilities['identity_probe'] == 'ready'
    assert result.timings.prompt_tokens is None
    assert result.timings.generated_tokens is None


@pytest.mark.skipif(os.name != 'posix', reason='fixture shebang requires POSIX')
def test_native_prompt_and_generated_token_counts_are_parsed(tmp_path):
    binary = executable(
        tmp_path,
        "print('bounded answer')\n"
        "print('llama_perf_context_print: prompt eval time = 12.34 ms / 13 tokens (0.95 ms per token, 1053.48 tokens per second)', file=sys.stderr)\n"
        "print('llama_perf_context_print: eval time = 84.00 ms / 7 runs (12.00 ms per token, 83.33 tokens per second)', file=sys.stderr)\n",
    )

    result = m.LlamaCppAdapter(binary).execute(request(tmp_path))

    assert result.success
    assert result.output == 'bounded answer\n'
    assert result.timings.prompt_tokens == 13
    assert result.timings.generated_tokens == 7
    assert result.timings.prefill_tps == pytest.approx(1053.48)
    assert result.timings.decode_tps == pytest.approx(83.33)


@pytest.mark.skipif(os.name != 'posix', reason='fixture shebang requires POSIX')
def test_generated_prompt_prefix_is_not_deleted_in_rendered_mode(tmp_path):
    binary = executable(tmp_path, "sys.stdout.write(sys.argv[sys.argv.index('-p')+1]+' completion  ')\n")
    req = request(tmp_path)
    result = m.LlamaCppAdapter(binary).execute(req)
    assert result.output == req.prompt + ' completion  '
    req.prompt_mode = 'runtime-default'
    assert m.LlamaCppAdapter(binary).execute(req).output == 'completion'


@pytest.mark.skipif(os.name != 'posix', reason='fixture shebang requires POSIX')
def test_bad_settings_return_native_failure_without_launching_inference(tmp_path):
    binary = executable(tmp_path, "raise AssertionError('must not execute')\n")
    req = request(tmp_path)
    req.context_tokens = 257
    with patch.object(m.subprocess, 'run') as run:
        result = m.LlamaCppAdapter(binary).execute(req)
    run.assert_not_called()
    assert not result.success
    assert result.error_class == FailureClass.RUNTIME


@pytest.mark.skipif(os.name != 'posix', reason='fixture shebang requires POSIX')
def test_unsupported_completion_binary_fails_without_fallback(tmp_path):
    binary = executable(tmp_path, "sys.stderr.write('unsupported option'); sys.exit(2)\n")
    result = m.LlamaCppAdapter(binary).execute(request(tmp_path))
    assert not result.success
    assert result.raw_exit_code == 2
    assert result.error_class == FailureClass.EXECUTION


@pytest.mark.skipif(os.name != 'posix', reason='fixture shebang requires POSIX')
def test_invalid_utf8_output_is_failure_not_mojibake_or_exception(tmp_path):
    binary = executable(tmp_path, "import os; os.write(1,b'\\xff')\n")
    result = m.LlamaCppAdapter(binary).execute(request(tmp_path))
    assert not result.success
    assert result.output is None
    assert result.error_class == FailureClass.EXECUTION


@pytest.mark.skipif(os.name != 'posix', reason='fixture shebang requires POSIX')
def test_timeout_is_preserved_on_actual_fixture_process(tmp_path):
    binary = executable(tmp_path, "import time; time.sleep(5)\n")
    req = request(tmp_path, timeout_seconds=0.1)
    result = m.LlamaCppAdapter(binary).execute(req)
    assert not result.success
    assert result.error_class == FailureClass.TIMEOUT
    assert result.timings.total_seconds < 3


@pytest.mark.parametrize('output_cap', [256, 257])
def test_rendered_context_must_leave_room_for_input(tmp_path, output_cap):
    req = request(tmp_path)
    req.max_tokens = output_cap
    with pytest.raises(ValueError, match='leave room'):
        m.LlamaCppAdapter(tmp_path / 'binary')._command(req)


def test_cpu_profile_cannot_request_gpu_offload_in_rendered_mode(tmp_path):
    with pytest.raises(ValueError, match='CPU resident'):
        command(tmp_path, options={'gpu_layers': 1})
