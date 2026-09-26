# Native runtime prerequisites (Phase 1)

The source declaration separates building the existing reference llama-cli from
running the existing CPU resident adapter. It does not install either stack.

`llama-cpp-build` names Bash, Git, CMake, Ninja and the C/C++ toolchain used by the
single-job build in the existing reference-vessel recipe. The deployment owner
still binds the exact external llama.cpp revision and its native CMake inputs.
This declaration is not permission to rerun legacy cloud-init or replace private
Fleet lifecycle workflows. Compiler version resolution remains native; no second
package manager or copied external dependency graph is introduced.

`llama-cpp-cpu-runtime` requires Python and the selected llama-cli executable,
not a compiler on every runtime host. The reviewed model artifact and required
Python dependencies remain independently provisioned. These initial profiles
cover the Linux/glibc/amd64 reference path. They do not establish Android, Jetson,
CUDA, Vulkan or Metal support; those require their own native source bindings,
adapter capabilities and actual workload evidence. Merely observing a GPU does
not select an acceleration backend.

## Runtime preflight

`LlamaCppAdapter.identity()` now probes the fixed `--version` command with a
five-second subprocess deadline and a 16 KiB combined stdout/stderr limit. A
caller may lower, not raise, that deadline. It never invokes a shell. Ordinary
child processes in the probe's POSIX process group are killed during cleanup,
including when a wrapper exits but its child holds the output pipe open.

Missing, denied, unusable, timed-out, empty/invalid-version and excessive-output
results are distinguished in `RuntimeIdentity.capabilities.identity_probe`.
They return `ValidationState.UNKNOWN`, no build/version identity and no supported
backend claim. `execute()` refuses to launch inference when this preflight is
not supported, even if the model path exists. Wrong runtime/strategy and missing
model behavior remain separate; scheduler exploration rules are unchanged.

Successful identity retains the existing SHA-256-derived build ID, now computed
in chunks rather than loading the whole executable into memory. The regular
binary is limited to 512 MiB, and a changed device/inode/size/mtime across the
probe invalidates the identity. Relative paths resolve against the caller's
working directory rather than accidentally searching PATH. This is neither an
immutable executable handle nor a digest of all shared libraries or dependencies.

The version subprocess deadline does not impose a hard deadline on filesystem
I/O. It is separate from the existing inference request timeout and output path.
This change does not bound inference output, validate GGUF content or authenticate
a version banner. The executable must already come from a trusted, reviewed
installation. Process-group cleanup is not a sandbox against hostile code that
escapes its session. Unsupported probe platforms fail as unknown.

A successful result remains `PREFLIGHT` with `probe` provenance. It means the
CPU resident prerequisites are available, not that a real model loaded, a GPU
was used, execution succeeded or a benchmark passed. Execution and benchmark
validation retain their existing meanings.

## Tests and deployment

The existing Python 3.11/3.12 CI runs `tests/test_llama_preflight.py` alongside the
other tests. These tests execute tiny local fixture processes to exercise success,
failed version commands, permission errors, timeouts, output limits, inherited
pipes and inference gating. Their model file is explicitly synthetic. No real
llama.cpp checkout/build, model download, accelerator or cloud credential is
required, and fixture results are not live device qualification.

The existing requirements CI validates the new declaration against the unchanged
public CUE contract. A private consumer must review and advance its source pin,
add the selected profiles to its own mapping, and qualify the real installation.
Merging this source does not update a deployed runtime, grant a runner role, or
silently refresh Fleet's separately pinned public contract and private exports.
Optional-acceleration contracts and actual clean-host/device evidence remain #86
and the corresponding private integration work.
