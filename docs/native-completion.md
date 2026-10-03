# Prepared native completion

The existing `LlamaCppAdapter` now accepts explicit `seed`, `context_size`,
`threads`, and optional `threads_batch` profile options. Integers are validated
without coercion; zero/negative thread and context sentinels and random seeds are
rejected when those controls are supplied. Existing profiles without these options
keep their previous invocation behavior.

## Opt-in prepared prompts

A profile with `prepared_completion: true` requires an explicit seed, context size,
and thread count. The caller supplies an already rendered prompt. The adapter:

- writes exact UTF-8 prompt bytes to a temporary file, closed before launch,
  and uses upstream binary-file input (`-bf`) rather than text-file input;
- disables conversation, prompt display, escape processing and context shifting;
- requests explicit native performance counters rather than assuming defaults;
- closes standard input, requests offline operation, and passes GPU layer zero
  explicitly for the CPU case rather than inheriting upstream auto-offload;
- removes ambient `LLAMA_ARG_*` settings from the child environment;
- preserves completion output without removing a matching prompt prefix or
  stripping leading/trailing content;
- removes the temporary prompt after success, failure, timeout or exception.

This is invocation control, not a process/credential sandbox or a memory limit.
The existing timeout is a direct-child timeout; Fleet still owns whole-tree cleanup,
resource admission, persistent credentials and device exclusivity. Completion text
is decoded as UTF-8 using Python's text-mode newline handling. Input bytes are not
normalized. No secure-erasure guarantee is claimed for the temporary file.

## Select the correct upstream interface

At upstream commit `7fe450e19305b828c199d602c23a8337aaa1f03b`, the prepared-prompt
flags belong to `llama-completion`; the separate chat-focused `llama-cli` must not
be assumed compatible. Older pins may provide the completion interface under the
CLI name. Supply the exact qualified executable; the adapter never swaps binaries
or retries with weaker flags after failure.

Upstream text-file (`-f`) handling strips one trailing newline. That changes a
prepared prompt and can change its token count. The byte-preserving `-bf` path
is compared against a direct `-p` reference using a harmless public prompt.

Primary source: [pinned completion documentation](https://github.com/ggml-org/llama.cpp/blob/7fe450e19305b828c199d602c23a8337aaa1f03b/tools/completion/README.md).
The profile changes its existing identity because these controls live in `options`.
Old feasibility/performance records cannot silently certify the modified profile.

## Validation layers

`tests/test_llama_invocation.py` exercises argv, exact prompt files, environment
controls, failure handling and cleanup using deliberately synthetic child processes.
These tests do not run a model.

`tests/test_llama_native_completion.py` is a separate opt-in CPU smoke using the
pinned source and 135M GGUF listed in `tests/fixtures/native-completion.json`. The
existing public CI builds the completion target and verifies the model hash before
comparing adapter output and reported input count to an independently constructed
upstream argv-prompt invocation and
repeating the same seeded request in a fresh process. Without explicitly supplied
paths the two tests skip; a skip is not native qualification.

Neither layer establishes tokenizer/template parity for arbitrary models, task
competence, a safe browser footprint, or Anchorage/Kestrel qualification. No DBD
corpus, private Fleet evidence, held-out questions or evaluator labels are copied
into this public smoke. The prompt is a generic public greeting, not a quality test.

Before a scientific development run, the caller must bind the exact model and
native tokenizer/template, count the final input including special tokens, reserve
output space within context, use the common scheduler admission, and retain ordinary
started/terminal evidence under the approved physical execution boundary. Explicit
seeding does not guarantee bit-identical outputs across different backends/builds.
