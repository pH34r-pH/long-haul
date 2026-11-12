# Observable harness trajectories

The harness-neutral contracts in `long_haul.work.harness` describe execution and
candidate provenance. They do not change WorkContract authority, certify task
acceptance, or introduce a scheduler or durable workflow engine.

## Current executable scope

The current slice implements strict immutable attempt/action records, a small
adapter protocol, Pi RPC event projection, keyed metadata normalization, and a
deterministic scripted fixture adapter, and a pinned Pi subprocess adapter with a
separate writable Docker/gVisor worker profile. The scripted fixture performs no tool execution,
starts no subprocess, and claims no model, sandbox, or real Pi qualification.

The required `Pi worker qualification` job is the real subprocess/worker gate.
Prior source `a6cce652` passed eight actual Pi cases and six evaluator prerequisite
cases in [run 43099422919](https://github.com/pH34r-pH/long-haul/actions/runs/43099422919).
The current source must qualify again on its exact head. Independent evaluator
acceptance remains separate from both.
The existing legacy Pi prompt/tool helper is not replaced by this slice.

## Admission and privacy

An authenticated caller supplies the exact WorkContract, WorkSession, harness
package/source/artifact/configuration identities, and shared resource-policy
admission. Each observed action requires current `annotate` permission for the
`operational-trace` purpose, bound to the session, crew, mission, contract, and
repository. Expired, revoked, changed, or cross-scope admission fails closed.

Normalization happens before persistence. Only allowlisted fields survive:
action class, keyed native correlation ID, sequence, timestamps, outcome, safe numeric
range metadata, keyed fingerprints, and admitted evidence references. Paths are
opaque by default and may be disclosed only through an explicit normalization
policy. Source contents, queries, command text, environment values, tool output,
private reasoning, and prompts are never copied into ordinary semantic events.

The caller supplies a high-entropy HMAC key outside event history. A public key
reference and key tag are included in normalization identity; changing key
material changes comparability. Publication of operational traces requires its
own resource-policy decision and is not implied by annotation permission.
Exact native call IDs stay in private in-memory correlation state by default;
copying them to history requires explicit authenticated metadata disclosure.
If annotation permission expires or is revoked during a call, the old admitted
history remains, but no argument-bearing completion is copied under a stale
receipt. The terminal result records the denial and still requires cleanup.

## Observable state

`PiTrajectoryProjector` correlates `tool_execution_start` and
`tool_execution_end` by native call ID. A completion without a start, duplicate
IDs, malformed actions, and unadmitted tools remain explicit failures. Read and
search arguments are canonicalized so equivalent relative/absolute workspace
paths and explicit/implicit defaults have comparable keyed signatures.

`agent_end` is not terminal. `agent_settled` is the canonical session-level end;
retries, compaction failures, provider failure, cancellation, crashes, output
record limits, and externally observed deadlines remain distinct. Interrupted
actions receive an explicit unknown outcome. A successful edit/write result
does not prove mutation: only trusted before/after candidate references can
classify measured state change. Model-supplied result fingerprints are ignored.

The projector is an observer, not a pre-execution interlock. The real adapter
must enforce the same capability/scope admission before tool execution. The
supported native tool projection declines `shell`, `test`, and unknown
capabilities; a narrow test capability cannot silently become native Bash.

## Writable worker profile

`DockerGVisorPiWorker` extends the maintained evaluator executor's Docker control,
verified gVisor bundle, containment arguments, inspection, and attach/cleanup
seams. Its distinct environment declares read/write workspace access. Evaluation
keeps its separate read-only Python profile. Both require at least 128 host tasks
and 256 MiB; the image, runtime files, memory/swap, CPU, PID limit, mounts, exact
command, image environment, and network-none configuration are checked before
startup. No Docker socket, host Git tree, credential directory, or evaluator
material enters the worker.

The first worker supports `read`, `write`, and `edit`, without native executable
or search tools. Only authenticated registry-admitted regular files are projected.
Every depth of `.pi` and `.git`, links, traversal, ambiguous Unicode/expanded path
spellings, and control/proc targets decline. Reads must exist, preventing native
filename fallback from selecting a different resource. An explicit pinned
`tool_call` extension pauses before native filesystem execution and uses Pi's
maintained RPC extension UI to request host admission. This machine interlock is
independent of `--no-approve`; `--no-extensions` permits this sole explicit `-e`.

The host binds each response to its exact attempt/session, pending native call,
normalized operation and resource/content identity. It rechecks the pinned
WorkContract and shared current policy. Reads require `read` and `ai-augment`;
edits additionally require `derive`, and writes require `derive`. Annotation
permission cannot authorize execution. Unknown, malformed, duplicate, cancelled,
or timed-out interlocks deny. A completion without exactly one matching host
allow is invalid. Ordinary end events reference the execution admission receipt.

Writes and edits require a separate authenticated output/model policy. Changed
bytes receive a new derived ResourceIdentity and an operation-bound derivation
receipt. The original policy's content identity and grants are preserved.
`ResourcePolicyEvaluator.evaluate_all` checks the same requested action and
purpose against every source, initial task/context input, observed native-read
input and output-policy parent. Completion rechecks derivation rights; later
read/edit uses recheck all parents, including lifecycle, expiry and retention.
Fresh-session resume imports this typed lineage alongside the exact candidate;
changing an old profile's content hash cannot substitute for derivation.

An arbitrary prompt callback supplies no authority. The authenticated caller
must declare policies covering the complete exact task/context view, bound to
its keyed content fingerprint, context manifest and TaskDigest identity. The
host checks trace annotation plus current source READ/AI_AUGMENT before queueing
any text. Input history records only the template reference, keyed actual text
identity, task/context references, source decisions and requested/acknowledged
status. A denied or unacknowledged request never claims delivery. These initial
input policies also restrict every subsequent candidate derivation.
Within an attempt, a later prompt or native read cannot replace an earlier
policy for the same resource identifier with a different fingerprint. The host
denies that conflict before delivery so a later derivation cannot silently lose
the earlier input restriction; changed authority requires a fresh admission.

The agent's settings, model/auth configuration and system text are separately
read-only mounts inside a disposable writable lock/cache parent. The launcher
scrubs the child environment and disables automatic project context, packages,
extensions, MCP, skills, prompt templates, themes, downloads and version checks.
Project `.pi` files never reach startup. Configuration never evaluates source
`!command` values. The registry and HMAC keys remain in the host.

The exact npm lock is installed with lifecycle scripts disabled; maintained
`npm audit signatures` verifies registry signatures/provenance, and the signed
package subject is checked against the exact upstream revision. The installed
tree, official Node OCI digest, host Python/Pydantic, guard/control source,
worker implementation and deterministic provider fixture enter identity.

RPC uses bounded LF-only JSONL, nonblocking queued writes, bounded combined
output, monotonic duration, explicit prompt acknowledgement and one pending tool.
The worker process wall deadline includes Pi startup; trusted acquisition,
projection, Docker control and post-cleanup Git freeze are separately bounded
control costs. Unsupported token-metered contracts decline. Ended actions feed
the existing progress monitor; no-progress exhaustion terminates the attempt.
Optional EventStore integration appends normalized actions and interventions.

Cancellation clears queued messages and aborts the active run. Every path then
force-removes the entire container before any candidate freeze or resume receipt;
cleanup failure produces no such result. Trusted Git object construction merges
stopped projected bytes with the frozen base, preserving unprojected files, then
reuses the existing exact candidate freeze and independent evaluator.

If removal cannot be confirmed, the worker retains its workspace/control trees
and an owner-only private recovery receipt naming the exact container, attempt
and environment. The caller must keep the lease held; the executor refuses new
work or workspace reuse. Explicit `recover()` retries removal and returns the
existing HarnessCleanup proof only after observed success, then removes the
staging trees. The failed-state receipt remains immutable beside the recovery
proof. Recovery paths and container-management details do not enter ordinary
semantic history.

## Finish and resume

Finalizing an attempt requires an exact trusted process-tree cleanup receipt.
Fixture cleanup cannot substitute for a real sandbox receipt. Candidate
references from failed or cancelled attempts remain discarded history; even a
frozen successful candidate is explicitly `not_evaluated`.
Imported results enforce these same cleanup, identity, and candidate-state
invariants and are revalidated before resume.

Resume uses the existing WorkCheckpoint, TaskDigest, and WorkSession functions.
It requires exact prior identity and cleanup, an unchanged WorkContract, a fresh
session and attempt, and remaining `max_attempts` budget. The new attempt retains
the previous result and checkpoint fingerprints without copying conversation.

Semantic records append through the existing EventStore as observations.
Completed actions feed the existing ProgressMonitor, which emits ordinary
`work_progress_intervention` records. No transcript store or new event authority
is introduced.

## Conformance

```sh
python -m pytest -q tests/test_harness_trajectory.py tests/test_pi_worker.py tests/test_worker_candidate.py tests/test_pi_derivation.py tests/test_pi_input.py tests/test_pi_recovery.py
```

The fixtures cover deterministic replay, canonical arguments, private canaries,
scope escapes, malformed and unknown actions, repeated failures, measured
mutation, recovery, expired/revoked policy, cancellation, cleanup, bounded
resume, and separation of fixture evidence from real execution.

The hosted job installs the signed pinned Pi package and runs a deterministic
OpenAI-compatible provider on the same network-none sandbox's loopback. It
exercises actual Pi read/edit RPC and then the separate public/sealed **public
synthetic** evaluator fixture, plus denial, provider failure, deadline and
cancellation. This qualifies subprocess/tool/protocol mechanics only. It is not
LLM competence, private held-out evaluation, or a successful #64 model benchmark.
Private evaluator commitments/export policy remain the separate #245 gate.
