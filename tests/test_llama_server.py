"""Loopback HTTP fixtures; no model execution or host qualification."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest

from long_haul.adapters.llama_server import LlamaServerAdapter
from long_haul.models import InferenceProfile, ModelArtifact
from long_haul.runtime import (
    ExecutionRequest, FailureClass, ProfileValidation, RuntimeIdentity,
    ValidationDepth, ValidationState,
)


@contextmanager
def server_fixture(transform=lambda path, reply: reply):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, body))
            if self.path == '/tokenize':
                reply = {'tokens': [1, 2, 3]}
            elif self.path == '/apply-template':
                reply = {'prompt': 'rendered café\n'}
            else:
                reply = dict(content='  café [end of text]\n', tokens=[5, 6],
                             tokens_predicted=3, tokens_evaluated=len(body['prompt']),
                             stop=True, truncated=False, stop_type='eos',
                             generation_settings={key: body[key] for key in
                                                  ('n_predict', 'seed', 'temperature')})
            reply = transform(self.path, reply)
            data = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except BrokenPipeError:
                pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield server.server_port, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def setup(port, *, observe=None, **kwargs):
    profile = InferenceProfile(
        id='test-server', runtime_id='llama.cpp', strategy='resident',
        artifact=ModelArtifact(foundation='fixture-only'), participating_resources=['fixture-cpu'],
        options={'transport': 'llama-server', 'server_port': port, 'context_size': 64},
    )
    binding = ProfileValidation(
        runtime=RuntimeIdentity(runtime_id='llama.cpp', build_id='synthetic-server'),
        profile_id=profile.id, artifact_key=profile.artifact.key,
        resources=profile.participating_resources, strategy=profile.strategy, options=profile.options,
        state=ValidationState.UNKNOWN, provenance='simulated', rationale='fixture, not qualification',
    )
    req = ExecutionRequest(request_id='fixture', profile=profile, prompt='prepared prompt',
                           context_tokens=64, prompt_mode='rendered', seed=0,
                           temperature=.7, max_tokens=16, timeout_seconds=2,
                           allow_unknown_runtime=True)
    return LlamaServerAdapter(port, binding, observe=observe, **kwargs), req


def test_clean_content_and_predicted_count_are_preserved_without_heuristics():
    retained = []
    with server_fixture() as (port, calls):
        adapter, req = setup(port, observe=lambda path, obj: retained.append((path, obj)))
        result = adapter.execute(req)
    assert result.success
    assert result.output == '  café [end of text]\n'
    assert result.timings.prompt_tokens == 3
    assert result.timings.generated_tokens == 3  # Includes native terminal sample.
    assert result.timings.load_seconds is None and result.timings.ttft_seconds is None
    assert [path for path, _ in calls] == ['/tokenize', '/completion']
    payload = calls[1][1]
    assert payload['prompt'] == [1, 2, 3] and payload['seed'] == 0
    assert payload['cache_prompt'] is False and payload['return_tokens'] is True
    assert payload['stream'] is False and payload['stop'] == []
    assert calls[0][1]['add_special'] is False and calls[0][1]['parse_special'] is True
    assert len(retained[-1][1]['tokens']) == 2


@pytest.mark.parametrize('changes', [
    {'stop': False}, {'truncated': True}, {'stop_type': 'word'}, {'content': None},
    {'tokens_predicted': 17}, {'tokens_predicted': True}, {'tokens_predicted': -1},
    {'tokens_evaluated': 4}, {'tokens': [True]}, {'tokens': [5, 6, 7, 8]},
    {'generation_settings': None}, {'generation_settings': {'seed': 1}},
])
def test_malformed_native_response_never_becomes_a_success(changes):
    def transform(path, reply):
        return {**reply, **changes} if path == '/completion' else reply
    with server_fixture(transform) as (port, _):
        adapter, req = setup(port)
        result = adapter.execute(req)
    assert not result.success and result.output is None
    assert result.error_class is FailureClass.EXECUTION
    assert result.timings.generated_tokens is None


@pytest.mark.parametrize('raw', [b'[]', b'{"tokens":[1],"tokens":[2]}',
                                b'{"tokens":NaN}', b'{"tokens":1e999}', b'\xff', b'{'])
def test_invalid_json_is_rejected(raw):
    with server_fixture(lambda *_: raw) as (port, calls):
        adapter, req = setup(port)
        assert not adapter.execute(req).success
        assert len(calls) == 1


def test_context_is_checked_using_native_tokenization_before_generation():
    with server_fixture() as (port, calls):
        adapter, req = setup(port)
        req.max_tokens = 62
        result = adapter.execute(req)
    assert not result.success and len(calls) == 1


def test_request_and_bound_profile_cannot_drift():
    with server_fixture() as (port, calls):
        adapter, req = setup(port)
        req.profile.options['context_size'] = 128
        assert not adapter.execute(req).success
        assert not calls


def test_unknown_binding_requires_explicit_exploration_and_is_not_promoted():
    with server_fixture() as (port, calls):
        adapter, req = setup(port)
        req.allow_unknown_runtime = False
        assert not adapter.execute(req).success
        assert not calls
        assert adapter.validate(req.profile).state is ValidationState.UNKNOWN
        req.allow_unknown_runtime = True
        assert adapter.execute(req).success
        assert adapter.validate(req.profile).state is ValidationState.UNKNOWN


def test_cli_binding_and_preflight_cannot_certify_a_server():
    adapter, _ = setup(1234)
    binding = adapter.binding.model_copy(deep=True)
    binding.options['transport'] = 'cli'
    with pytest.raises(ValueError, match='server-specific'):
        LlamaServerAdapter(1234, binding)
    binding = adapter.binding.model_copy(update={'state': ValidationState.SUPPORTED})
    with pytest.raises(ValueError, match='measured execution'):
        LlamaServerAdapter(1234, binding)
    binding = binding.model_copy(update={'depth': ValidationDepth.EXECUTION, 'provenance': 'measured'})
    assert LlamaServerAdapter(1234, binding).binding.state is ValidationState.SUPPORTED


def test_retention_failure_propagates_without_claiming_completion():
    def reject(*_):
        raise RuntimeError('test retention failed')
    with server_fixture() as (port, calls):
        adapter, req = setup(port, observe=reject)
        with pytest.raises(RuntimeError, match='retention failed'):
            adapter.execute(req)
        assert len(calls) == 1


def test_callback_cannot_mutate_the_response_used_by_the_adapter():
    def mutate(_, obj):
        obj.clear()
    with server_fixture() as (port, _):
        adapter, req = setup(port, observe=mutate)
        assert adapter.execute(req).success


def test_native_template_is_returned_verbatim():
    with server_fixture() as (port, calls):
        adapter, _ = setup(port)
        result = adapter.apply_template([{'role': 'user', 'content': 'hello'}],
                                        deadline=time.monotonic() + 2)
    assert result == 'rendered café\n' and calls[0][0] == '/apply-template'


def test_schema_is_an_explicit_profile_choice():
    with server_fixture() as (port, calls):
        adapter, req = setup(port)
        req.profile.options['json_schema'] = {'type': 'object'}
        adapter.binding.options['json_schema'] = {'type': 'object'}
        assert adapter.execute(req).success
    assert calls[-1][1]['json_schema'] == {'type': 'object'}


def test_float32_sampling_rounding_is_allowed_but_wrong_temperature_is_not():
    for temperature, success in [(0.699999988079071, True), (0.8, False)]:
        def transform(path, reply):
            if path == '/completion':
                reply['generation_settings']['temperature'] = temperature
            return reply
        with server_fixture(transform) as (port, _):
            adapter, req = setup(port)
            assert adapter.execute(req).success is success


def test_one_deadline_covers_tokenization_and_completion_without_retry():
    def slow(path, reply):
        time.sleep(.06)
        return reply
    with server_fixture(slow) as (port, calls):
        adapter, req = setup(port)
        req.timeout_seconds = .1
        result = adapter.execute(req)
    assert not result.success and result.error_class is FailureClass.TIMEOUT
    assert len(calls) == 2
    assert 'reconcile' in result.error_detail


@pytest.mark.parametrize('limit', [True, -1, 0, 1.2])
def test_invalid_byte_limit(limit):
    with pytest.raises(ValueError):
        setup(1234, max_json_bytes=limit)


def test_oversized_response_is_rejected_and_not_retained():
    retained = []
    with server_fixture(lambda *_: {'padding': 'a' * 2048}) as (port, _):
        adapter, req = setup(port, max_json_bytes=512, observe=lambda *x: retained.append(x))
        assert not adapter.execute(req).success
        assert not retained


@pytest.mark.parametrize('port', [0, 65536, True, '8080', 'example.org'])
def test_port_cannot_be_an_arbitrary_network_destination(port):
    with pytest.raises(ValueError):
        setup(port)
