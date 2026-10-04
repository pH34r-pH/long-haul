"""Bounded JSON adapter for a caller-owned, loopback-only llama-server process.

The owner binds exact runtime/model/profile evidence and manages the process/lease.
This adapter never launches a server, follows redirects, retries, or promotes a
health check into qualification. CLI behavior and historical records are unchanged.
"""
from __future__ import annotations

from copy import deepcopy
import http.client
import json
import math
import time
from collections.abc import Callable

from ..models import InferenceProfile, canonical_json
from ..runtime import (
    ExecutionRequest, ExecutionResult, FailureClass, ProfileValidation,
    RuntimeIdentity, Timing, ValidationDepth, ValidationState,
)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("local server request deadline expired")
    return remaining


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid native token counter")
    return value


def _tokens(value: object) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("invalid native token vector")
    return [_integer(token) for token in value]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate native response field")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("nonfinite native response")


def _finite_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite native response")
    return number


def _read_json(response, sock, deadline: float, limit: int) -> dict:
    if response.status != 200:
        raise ValueError(f"local server HTTP status {response.status}")
    if response.getheader('Content-Type', '').split(';')[0].strip() != 'application/json':
        raise ValueError("local server did not return JSON")
    data = bytearray()
    while not response.isclosed():
        sock.settimeout(_remaining(deadline))
        chunk = response.read1(min(65536, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("local server response exceeds byte limit")
    _remaining(deadline)
    result = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object,
                        parse_constant=_invalid_constant, parse_float=_finite_number)
    if not isinstance(result, dict) or 'error' in result:
        raise ValueError("invalid native response object")
    return result


def _prepared_context(request: ExecutionRequest) -> int:
    context = request.profile.options.get('context_size')
    if (request.prompt_mode != 'rendered' or type(context) is not int or context <= 0
            or request.context_tokens != context):
        raise ValueError("prepared request must match the owned server context")
    if request.temperature > 0 and request.seed is None:
        raise ValueError("stochastic request requires an explicit seed")
    return context


def _completion_payload(request: ExecutionRequest, token_ids: list[int]) -> dict:
    # Send IDs, not a string: upstream then adds neither a BOS nor a chat template.
    data = dict(prompt=token_ids, n_predict=request.max_tokens,
                temperature=request.temperature, seed=request.seed or 0,
                stream=False, return_tokens=True, cache_prompt=False,
                n_cache_reuse=0, n_cmpl=1, id_slot=0, stop=[], ignore_eos=False,
                samplers=['top_k', 'top_p', 'min_p', 'temperature'],
                top_k=40, top_p=0.95, min_p=0.05)
    schema = request.profile.options.get('json_schema')
    if schema is not None:
        if not isinstance(schema, dict):
            raise ValueError("profile json_schema must be an object")
        data['json_schema'] = deepcopy(schema)
    return data


def _validate_completion(reply: dict, request: ExecutionRequest, inputs: list[int]) -> None:
    if reply.get('stop') is not True or reply.get('truncated') is not False:
        raise ValueError("incomplete or context-truncated native response")
    if reply.get('stop_type') not in {'eos', 'limit'} or not isinstance(reply.get('content'), str):
        raise ValueError("invalid native completion disposition")
    predicted = _integer(reply.get('tokens_predicted'))
    if _integer(reply.get('tokens_evaluated')) != len(inputs):
        raise ValueError("native prompt accounting differs from submitted token IDs")
    if predicted > request.max_tokens or len(_tokens(reply.get('tokens'))) > predicted:
        raise ValueError("native output token accounting exceeds its allowance")
    settings = reply.get('generation_settings', {})
    if not isinstance(settings, dict):
        raise ValueError("native generation settings missing")
    expected = {'n_predict': request.max_tokens, 'seed': request.seed or 0}
    if any(type(settings.get(key)) is not int or settings[key] != value
           for key, value in expected.items()):
        raise ValueError("native server changed requested sampling settings")
    temperature = settings.get('temperature')
    if type(temperature) not in (int, float) or not math.isclose(
        temperature, request.temperature, rel_tol=1e-6, abs_tol=1e-7
    ):
        raise ValueError("native server changed requested temperature")


class LlamaServerAdapter:
    """Use the RuntimeAdapter contract with an externally owned single-slot server.

    ``binding`` is supplied by the trusted owner, never by a model/HTTP peer. Its
    server-specific profile includes transport and port. Socket reachability does
    not authenticate the process; caller ownership/exclusion remains mandatory.
    ``observe`` receives raw upstream JSON for an existing evidence sink, not a new
    receipt format. A timeout does not prove remote cancellation or free the lease.
    """
    runtime_id = 'llama.cpp'

    def __init__(self, port: int, binding: ProfileValidation, *,
                 observe: Callable[[str, dict], None] | None = None,
                 max_json_bytes: int = 1024 * 1024):
        if type(port) is not int or not 0 < port < 65536:
            raise ValueError("explicit loopback port required")
        if type(max_json_bytes) is not int or max_json_bytes <= 0:
            raise ValueError("positive JSON byte limit required")
        if (binding.runtime.runtime_id != self.runtime_id or not binding.runtime.build_id
                or binding.options.get('transport') != 'llama-server'
                or binding.options.get('server_port') != port):
            raise ValueError("server-specific owner binding required")
        if binding.state is ValidationState.SUPPORTED and (
            binding.depth not in {ValidationDepth.EXECUTION, ValidationDepth.BENCHMARK}
            or binding.provenance != 'measured'
        ):
            raise ValueError("supported server binding requires measured execution evidence")
        self.port, self.binding = port, binding.model_copy(deep=True)
        self.observe, self.max_json_bytes = observe, max_json_bytes

    def identity(self) -> RuntimeIdentity:
        return self.binding.runtime.model_copy(deep=True)

    def validate(self, profile: InferenceProfile) -> ProfileValidation:
        b = self.binding
        matches = (profile.runtime_id == self.runtime_id and profile.id == b.profile_id
                   and profile.artifact.key == b.artifact_key
                   and profile.participating_resources == b.resources
                   and profile.strategy == b.strategy
                   and canonical_json(profile.options) == canonical_json(b.options))
        if matches:
            return b.model_copy(deep=True)
        return b.model_copy(update={'state': ValidationState.UNSUPPORTED,
                                    'rationale': 'request differs from owned server profile'})

    def _post(self, path: str, payload: dict, deadline: float) -> dict:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(body) > self.max_json_bytes:
            raise ValueError("local server request exceeds byte limit")
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=_remaining(deadline))
        try:
            connection.request('POST', path, body, {'Content-Type': 'application/json'})
            sock = connection.sock
            sock.settimeout(_remaining(deadline))
            with connection.getresponse() as response:
                reply = _read_json(response, sock, deadline, self.max_json_bytes)
            if self.observe is not None:
                self.observe(path, deepcopy(reply))
            return reply
        finally:
            connection.close()

    def tokenize(self, text: str, *, deadline: float) -> list[int]:
        reply = self._post('/tokenize', dict(content=text, add_special=False,
                                             parse_special=True, with_pieces=False), deadline)
        return _tokens(reply.get('tokens'))

    def apply_template(self, messages: list[dict], *, deadline: float) -> str:
        prompt = self._post('/apply-template', {'messages': messages}, deadline).get('prompt')
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("native template did not produce a prompt")
        return prompt

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        started = time.monotonic()
        runtime = self.identity()
        failure = None
        validation = self.validate(request.profile)
        admitted = validation.state is ValidationState.SUPPORTED or (
            validation.state is ValidationState.UNKNOWN and request.allow_unknown_runtime)
        if not admitted:
            return ExecutionResult(request_id=request.request_id, runtime=runtime, success=False,
                                   error_class=FailureClass.RUNTIME, error_detail=validation.rationale)
        try:
            context = _prepared_context(request)
            deadline = started + request.timeout_seconds
            tokens = self.tokenize(request.prompt, deadline=deadline)
            if not tokens or len(tokens) + request.max_tokens > context:
                raise ValueError("native input plus output allowance exceeds context")
            reply = self._post('/completion', _completion_payload(request, tokens), deadline)
            _validate_completion(reply, request, tokens)
            _remaining(deadline)
        except TimeoutError:
            failure = (FailureClass.TIMEOUT, 'local request timed out; owner must reconcile server work')
        except (OSError, http.client.HTTPException):
            failure = (FailureClass.RUNTIME, 'local server transport failed; owner must reconcile work')
        except (ValueError, TypeError, RecursionError):
            failure = (FailureClass.EXECUTION, 'native response or request contract failed')
        timing = Timing(total_seconds=time.monotonic() - started)
        if failure is not None:
            return ExecutionResult(request_id=request.request_id, runtime=runtime, success=False,
                                   error_class=failure[0], error_detail=failure[1], timings=timing)
        # Upstream n_decoded: sampled/predicted tokens, including terminal samples;
        # NOT len(content), re-tokenized text, or CLI eval runs. Raw IDs stay in observe.
        timing.prompt_tokens = reply['tokens_evaluated']
        timing.generated_tokens = reply['tokens_predicted']
        return ExecutionResult(request_id=request.request_id, runtime=runtime, success=True,
                               output=reply['content'], timings=timing)
