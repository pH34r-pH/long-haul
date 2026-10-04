# Native completion and tokenizer smoke

This completes the real-model test portion of PR #197 against the request
contract merged in PR #198. It does not introduce `prepared_completion` as a
second profile flag or replace the current adapter. The older prompt-file
transport proposal is not part of this slice.

The existing read-only [CI workflow](../.github/workflows/ci.yml) builds
`llama-completion` and `llama-tokenize` from one exact upstream source revision.
The [public input manifest](../tests/fixtures/native-completion.json) retains
PR #197's SmolLM2-135M Q4_K_M revision/hash and pins the upstream tokenizer JSON,
configuration and embedded chat-template hash. Files are verified before use.
No private corpus, Fleet topology, credentials or held-out input is supplied.

## Checks

The [opt-in tests](../tests/test_llama_native_completion.py) exercise:

- current `ExecutionRequest` seed, context and `prompt_mode=rendered` controls;
- completion equality against independently specified direct upstream arguments;
- a fresh-process repeat with the same seed, source, model and CPU settings;
- native prompt count against the native tokenizer rather than a regex alone;
- exact native/reference token-ID parity for a greeting, Unicode, literal escapes,
  real newlines and whitespace, including rendered chat special tokens.

Reference rendering uses Jinja2's sandbox and the hash-pinned model template.
The reference tokenizer is the standard Hugging Face Tokenizers implementation,
not a second BPE implementation. The two tools are pinned in the smoke job and
are not added as product dependencies. Installed package versions are retained.

Ordinary unit runs skip when external assets are absent. The native job sets
`LONG_HAUL_REQUIRE_NATIVE_SMOKE=1`, so missing inputs fail rather than silently
qualifying a skipped test. There are at most four 16-token completion invocations;
other probes load vocabulary only. Native calls use one CPU thread, a 512-token
context and a 60-second per-call timeout. The test process has a 3 GiB address-space
ceiling and a five-minute outer bound. These are explicit smoke-test limits,
not minimum resource requirements or browser fit claims.

## Evidence and limitations

CI retains the input manifest, upstream source revision, built executable hashes,
build/version logs, installed package versions, per-check observations and JUnit
results, including failures. Completion results use the existing runtime records.
Retain important results in the owning evidence history before convenience
artifacts expire. This job does not create scientific attempt receipts or replace
the Compiler/Fleet lifecycle.

A pass establishes behavior for the exact tested build, conversion and probes.
It is not universal tokenizer equivalence, instruction competence, a benchmark,
GPU evidence, native Windows qualification or permission to run a private study.
The conversion publisher's upstream lineage is not inferred to equal every later
upstream weight revision merely because these token IDs match.

The native performance log's evaluation `runs` count is **not assumed to be the
number of emitted tokens**. Existing `Timing.generated_tokens` parser semantics
need separate real-output validation before that field can enforce a scientific
output budget. These tests validate the prompt count and preserve raw reference
stderr rather than treating an unverified output counter as accurate.

Cold loading, CPU settings, conversion and runtime identity must remain explicit
when this evidence is consumed. Matching seeds do not promise cross-backend
bitwise equality. Shared Scheduler admission, model/profile resource qualification,
foreground ownership and the existing started/terminal retention path remain
requirements for actual development experiments.
