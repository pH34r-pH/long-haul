# Native JSON inference qualification

The `LlamaServerAdapter` uses the existing `RuntimeAdapter` request/result contract
against a caller-owned, single-slot `llama-server` on numeric IPv4 loopback. It adds
no server implementation, persistent service, inference gateway or dependency.

## Ownership and admission

The process owner supplies an exact `ProfileValidation` bound to the server build,
model artifact, placement and options, including `transport: llama-server` and the
selected `server_port`. A CLI qualification does not certify this profile. A
SUPPORTED binding requires measured execution evidence; an UNKNOWN smoke candidate
requires explicit exploration permission. Success never promotes the binding.

The caller remains responsible for authenticating/exclusively owning the local
process, current model/runtime identity, memory/CPU limits, workload isolation,
server startup and cleanup. A loopback socket or health response cannot prove these
properties. This adapter is not a public endpoint or a remote-worker implementation.

## Request and response semantics

Prepared text is sent through upstream `/tokenize` with `add_special: false` and
`parse_special: true`. The resulting IDs are checked against the context allowance
and sent to `/completion` as an integer array. Upstream then adds neither BOS nor a
chat template. `/apply-template` is available for callers that qualify native chat
rendering. The existing tokenizer-parity probes remain authoritative for their
limited inspected inputs, not every model or template.

Calls explicitly disable streaming and prompt-cache reuse, select slot zero, and
request returned token IDs. Sampling uses the declared temperature and seed (zero
for an omitted greedy seed), top-k 40, top-p 0.95, min-p 0.05 and that explicit
sampler chain. These are interface settings, not scientifically optimal choices;
retain the server's generation settings with each observation. An optional profile
`json_schema` enables upstream constrained output and must be held constant across
comparisons where syntax is not the experimental variable.

Content is the JSON `content` string, unchanged: no prompt-prefix trimming, EOS-text
removal or whitespace stripping. A literal marker produced by a model is not a CLI
status marker to erase. Incomplete, truncated, inconsistent or over-budget responses
are failures. Float32 temperature roundoff is checked with declared numerical
tolerances rather than exact decimal equality.

`Timing.prompt_tokens` records `tokens_evaluated`, checked against the submitted
IDs. `Timing.generated_tokens` records native `tokens_predicted` (`n_decoded`),
including terminal samples where upstream counts them. It is not CLI eval runs,
character length, re-tokenized text length or necessarily the returned token-vector
length. The optional observation sink retains the exact upstream response and raw
IDs so these quantities remain distinct. Historical CLI receipts are unchanged.

The total request timer includes tokenization, JSON transfer, inference and callback
work. It excludes caller-owned startup and any prior template rendering; those costs
must be retained separately for whole-attempt comparisons. No TTFT or load timing is
invented from non-streaming responses.

## Transport and failure limits

The standard-library HTTP transport has bounded request/response bytes, a shared
request deadline, strict UTF-8/JSON, and no redirects, proxies or automatic retries.
The default one-MiB JSON limit is a configurable transport bound, not an artifact
publication limit. Failed calls remain subject to the owner's retry budget.

A client timeout or disconnect **does not prove cancellation of server work**.
The owner must reconcile or terminate its process before releasing the execution
lease. The adapter does not expose stop/restart privileges. Observation callbacks
receive copies and may fail; they never certify scientific acceptance.

## Validation

Run ordinary HTTP fixtures with:

```sh
python -m pytest -q tests/test_llama_server.py
```

The existing read-only hosted CPU job also builds the same pinned upstream
`llama-server` target and runs `tests/test_llama_native_server.py`. It reuses the
existing checksum-bound public SmolLM2 model/tokenizer inputs, captures raw responses
and server logs, and reaps its own short-lived loopback process. The smoke tests
check template/token parity, clean content, accounting, seeded repetition with cache
reuse disabled, schema-parseable output and refusal before context overflow.

These are native interface checks, not DBD competence results, Fleet/Windows/Jetson
qualification, scientific admission or public Flash deployment. Scored development
still uses DSL's reviewed protocol and the existing Compiler/Fleet attempt path.

## Pinned upstream references

- [Server API](https://github.com/ggml-org/llama.cpp/blob/7fe450e19305b828c199d602c23a8337aaa1f03b/tools/server/README.md)
- [Completion counters and JSON serialization](https://github.com/ggml-org/llama.cpp/blob/7fe450e19305b828c199d602c23a8337aaa1f03b/tools/server/server-task.cpp)
