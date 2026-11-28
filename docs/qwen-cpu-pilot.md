# Qwen3 CPU correctness pilot

The focused runner in
[`experiments/qwen_cpu.py`](../src/long_haul/experiments/qwen_cpu.py) supports the
initial correctness work in [issue #311](https://github.com/pH34r-pH/long-haul/issues/311).
The scientific source owns the case inventory, input conversion, acceptance
criteria, campaign order, and immutable attempt records. This runner supplies a
single bounded raw-engine attempt and an existing `BenchmarkObservation`.
It does not acquire an execution reservation or implement a scheduler.

## Inputs and ownership

Use `TokenCase(id, prompt, prompt_ids, expected_ids, max_tokens)` with the exact
source-owned token vectors. The four original upstream cases all request
`max_tokens=64`; their shorter expected continuations arise from EOS. Passing
those cases establishes only exact agreement with that visible fixture.
Held-out evaluation, numerical/logit agreement, and task quality remain separate.

`RawEngineSpec` selects `opus` or `neon`, an existing binary path, an existing
asmbin path and its SHA-256, and a worker count. Binary digests are fixed to the
published engines at upstream revision
`2d53727ebb7a6e5fedd344298b405de50bdff1bc`. The runner does not download or
redistribute their bytes. Model preflight verifies the selected digest, exact
asmbin v2 length and Qwen3-0.6B shape, normalization, RoPE, and tokenizer flags.

The supplied `InferenceProfile` must use `qwen3-raw-hex-opus` or
`qwen3-raw-hex-neon`, with `binary_sha256`, `model_sha256` and integer `threads`
in its options. Retain tokenizer, conversion, model revision and quantization
identity in the source-owned artifact/profile. The raw binaries cannot be
labelled `llama.cpp`. A shared Q8 label does not establish equal quantized
values: the upstream asmbin exporter and GGML quantizer differ.

## Bounded execution

The caller supplies `ProcessLimits` with explicit wall seconds, CPU seconds,
address-space bytes, a combined stdout/stderr byte cap, and selected CPU IDs.
Preflight requires Linux x86-64 and AVX2/FMA/F16C on every selected CPU. It checks
the caller's allowed affinity and, where exposed at `/sys/fs/cgroup/cpu.max`,
the current cgroup quota. Missing quota information stays unavailable.

The runner launches a new session with a sanitized environment. An isolated
stdlib Python bootstrap applies resource limits and initial affinity before
`execve` of the verified binary. The qualified runtime must therefore bind that
Python interpreter too. Output collection is bounded and uses one wall deadline.
Every exit path signals remaining process-group members and reaps the direct
leader. The receipt records that cleanup scope explicitly.

The enclosing trusted container/cgroup owner must enforce aggregate memory,
PID and network constraints, supply read-only assets, handle cancellation and
escaped descendants, prove the complete workload stopped, retain evidence,
and release the reservation. Process-group cleanup is not proof of that wider
lifecycle. Run memory-heavy arms sequentially within their admitted envelope.

```python
from long_haul.experiments.qwen_cpu import run_raw_engine

# spec, case, limits and profile come from the reviewed scientific source.
with evidence_path.open("x", encoding="utf-8") as output:
    attempt = run_raw_engine(spec, case, limits, profile)
    output.write(attempt.model_dump_json(indent=2) + "\n")
```

Use a fresh evidence path for each attempt and retain the source request with
it. Preflight rejection produces no model execution or benchmark observation.
After launch, nonzero exit, timeout, output limit, invalid evidence and exact
token disagreement remain explicit unsuccessful outcomes. A successful process
exit cannot satisfy the token comparison by itself.

## Baseline and timing boundaries

The existing
[`LlamaServerAdapter`](../src/long_haul/adapters/llama_server.py) additionally exposes
`execute_tokens(request, token_ids)`. It sends those IDs directly to the owned
loopback server without another tokenizer call, BOS insertion or chat template.
Its ordinary profile binding, context/timeout checks, generation settings and
response accounting still apply. The existing `observe` callback retains the
raw native reply, including output IDs. The caller retains the submitted IDs.

The server must be started, limited, identified and stopped by the same trusted
attempt owner inside the approved boundary. This adapter never launches it or
turns a timeout into proof the server stopped. An explicit-ID request measures
resident-server request time; the raw runner measures process spawn through
reaped exit, including its bootstrap and model load. These differ and cannot
directly produce a speedup ratio.

Raw `PROMPT_TOKENS` and `GEN_TOKENS` records must occur exactly once. Parsing
rejects missing, duplicate, malformed and out-of-vocabulary vectors. Exact
comparison checks the complete vectors, including length; a matching prefix is
insufficient. Byte-exact bounded stdout/stderr are retained as base64 even if
decoding or parsing fails.

The rich receipt retains engine-reported rates separately from controller wall
time. It leaves `BenchmarkObservation.decode_tps` and `prefill_tps` unavailable,
and `performance_eligible=false`. It does not import upstream EPYC rates or
declare a winning engine. The workload records the requested output allowance;
observed output IDs and counts remain in the detailed receipt.

Digest verification reads model bytes before launch. File-cache state is
therefore recorded as `uncontrolled-after-digest-verification`; a cold-cache
claim is not made (`Workload.cold=false`). A fresh process is not sufficient
evidence of cold storage/cache conditions. Later performance work needs matched
timing definitions, tensor comparison policy, repeats, and observed resources.

## Validation

```sh
python -m pytest -q tests/test_qwen_cpu.py tests/test_llama_server.py
python -m ruff check src tests
```

These tests use synthetic token records, loopback HTTP fixtures, and bounded
Python subprocesses. They check disagreement, malformed/duplicate evidence,
identity binding, environment stripping, output flooding, timeout/group cleanup,
address-space limits, and exact token submission. They execute no model and
provide no hardware qualification or scientific performance result.
