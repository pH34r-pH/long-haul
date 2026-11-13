# Frozen repository evaluation

Repository evaluation composes the existing WorkContract, EvidencePacket and event history. It introduces no scheduler, database, agent framework, or production authority. Ordinary executable/artifact predicates and historical schema-v1 evidence remain readable.

## Authority and identity

`RepositoryEvaluationProfileV1` is authenticated control-plane input. It pins the task, evaluator implementation, sandbox implementation, immutable OCI image, exact gVisor bundle, resource/network policy, and every check. Repository or model text cannot supply this admission.

The observed evaluator implementation reference includes its relevant source files and the Python, Pydantic and Pydantic Core version strings. This reference is not an attestation of loaded code or host integrity. Adding Pydantic Core to the reference changes the identity: existing pinned evaluator profiles and dependent private key references must be regenerated and readmitted before use; historical evidence remains historical.

`AcceptancePredicate.evaluator_reference` explicitly identifies a profile-owned check without putting sealed commands into WorkContract. It cannot coexist with an executable/artifact definition. The ordinary evaluator reports these predicates as unknown; only `RepositoryEvaluationRunner` resolves them under the exact profile references in the repository extension. This leaves ordinary command meanings unchanged.

Evaluation materializes the complete frozen candidate, including unchanged files outside the worker projection. Callers must now supply a `CandidateInputAdmission` from authenticated control for every candidate file before evaluation can start. Each entry binds the exact content digest and caller-attested complete policy lineage; every parent must currently allow both READ and EVALUATE for the exact evaluator, contract, mission, repository and attempt. The runner rechecks admission around each check and rejects missing, conflicting, expired or changed policy. Private evaluation accepts both the input and evidence admissions in `PrivateEvaluationAdmissions`. Existing callers must supply these admissions and regenerate the pinned evaluator implementation reference. Test fixtures use explicitly synthetic policies; they are not production admission.

The registry permits at most 4096 total lineage entries per candidate. The registry is a trusted-control assertion: this implementation does not authenticate its caller or prove that the asserted lineage is complete. Rechecking after a command prevents acceptance following a detected revocation, but cannot retract bytes already made visible during execution. Input-policy decisions are not yet recorded as a durable private receipt, and concurrent revocation, real private authority and sandbox containment remain separate qualification gates.

The evaluator includes input admission and sandbox readiness in its work-contract wall budget and aborts before starting another check once the budget is exhausted. This is a cooperative check, not a hard deadline: it cannot interrupt an in-progress policy decision, workspace operation or sandbox command. Commands retain their separately configured sandbox timeouts, and an external supervisor must enforce a hard aggregate deadline and verify cleanup.

The worker-visible profile contains requirement IDs/descriptions/classes and public check definitions. Sealed commands, input values and expected results remain outside the worker workspace and tool catalog. The frozen profile digest and safe requirement references preserve identity without disclosing those definitions.

| Class | Execution authority |
| --- | --- |
| PUBLIC | Frozen public SUT command/input; expected bytes checked externally |
| SEALED | Evaluator-owned command/input; expected bytes checked externally |
| POLICY | Trusted Git change-scope check, independent of candidate output |
| INSPECTION | Exact read-only candidate artifact fingerprint |

## Candidate lifecycle

`freeze_repository_candidate` reads exact Git objects, verifies base identity/ancestry, and records changed paths and file digests. Links, submodules, unsafe paths, oversized inputs and missing objects decline; nothing is silently omitted. Initial limits are 256 files, 4 MiB total content, and the existing Git provider's 1 MiB per blob. Larger or unsupported repository packages need an explicitly qualified profile rather than a partial-success claim.

The snapshot contains immutable bytes independent of the worker checkout. Evaluation materializes a new executor-owned directory and mounts it read-only. The worker receives neither that directory nor the Docker socket. A concurrent edit to the worker checkout cannot alter the frozen candidate. Real worker process/session cancellation and the full adversarial worker profile remain #244/#245 gates; immutable candidate separation does not claim those later capabilities.

## Maintained isolation

`SandboxAdapter` is replaceable. The qualified Linux/amd64 path uses Docker's OCI controls with a pinned gVisor runtime, following the existing stock-image receiver pattern. It uses the existing digest-pinned Python image rather than building an evaluator OS.

The trusted executor verifies gVisor binary/sidecar hashes, Docker runtime configuration, resolved image identity, read-only candidate/root, non-root UID, dropped capabilities, no-new-privileges, isolated PID/network, CPU/memory/swap/PID bounds, bounded tmpfs and disabled container logging. The qualified backend requires at least 256 MiB and a 128 host-task ceiling; gVisor's host stub processes count against this ceiling. A paired CI probe showed init/Python startup failing at 32 tasks and succeeding at 128 with the same 256 MiB memory bound. Every Docker command uses an empty executor-owned configuration directory and a minimal environment; container environment must equal the pinned image defaults plus the two declared Python settings. Acquisition occurs separately; execution uses `--pull=never`.

Sealed expected outputs and the check runner remain outside the untrusted candidate process. Only each current SUT input is sent over bounded stdin. There is no sealed fixture mount for candidate code to inspect. Python is selected by an absolute path from the immutable image with isolated startup; candidate PATH shims cannot replace it. A trusted bootstrap verifies the candidate entrypoint digest, emits a distinct stderr startup receipt and waits for executor acknowledgement before running the SUT. This orders startup proof before candidate output, including an output flood. Missing startup proof is an invalid execution, even if the launcher returns an exit code.

Wall time and combined stdout/stderr are bounded. Timeout/output exhaustion triggers container termination; every path requires forced container removal. A failed startup, containment check, cleanup, timeout, resource exhaustion or missing runtime yields invalid/inconclusive evidence. It never becomes a successful check. This Work host cannot qualify the required runtime; ordinary local tests label the fixture adapter as synthetic and skip real-isolation claims.

## Evidence and replay

`RepositoryEvidencePacketV1` wraps schema-v1 EvidencePacket with exact contract/task/base/candidate/context/evaluator/environment/check identities and sanitized outcomes. Packet validation rejects contradictory states and duplicate evidence. Candidate, contract, task, profile, environment or check changes invalidate prior certification. `validity=invalid` maps to WorkEvaluation INCOMPLETE; a valid completed check with wrong behavior maps to REJECTED. These states remain separate from deployment and publication.

Only content digests and bounded metadata enter the public packet; sealed stdin, expected output, raw stdout/stderr and credentials do not. `repository_evidence_event` writes the packet through the existing append-only EventStore. Failed/invalid attempts are retained the same way as successful attempts. The checked-in JSON Schemas allow Compiler to pin and package these records without another evidence store.

The experimental private-summary path retains contract, attempt and predicate identifiers in its redacted payload. Before emitting a public event, trusted control must independently classify the exact identifiers as public and provide that classification along with exact-summary distribution admission. This is not automated secret detection: a mistaken or self-asserted classification can still disclose sensitive identifiers, so production classification and adversarial publication qualification remain open. Raw private evidence remains in its separately admitted private sink.

The experimental Key Vault adapter now separates `prepare` from `freeze`. Trusted control prepares in-memory keys and an intent, persists the intent in a private durable POSIX recovery directory, and independently admits its fingerprint in scheduler state before supplying a `KeyWriteGrant`. A synced one-shot claim is created before the provider write. A crash or ambiguous response leaves the admitted intent and claim for controlled inspection; the same intent must not be retried or silently removed. The intent stores a secret locator and identity fingerprints, not key material. Existing adapter callers must migrate and readmit key references because the adapter identity changed.

The adapter's read-only `recover` reloads the claimed intent against the independently retained fingerprint, requires current READ policy, and lists and reads one exact provider version whose keyed evaluator fingerprint matches the intent. It checks the version census before and after readback; a missing, changed or multiple-version result remains ambiguous and does not trigger a retry or deletion. Recovery requires Key Vault list and get permissions. These point-in-time checks do not fence concurrent provider changes, guarantee a hard network deadline, or prove provider response authenticity independently of the trusted SDK client.

Intent-record open and validation errors use generic messages rather than echoing recovery locators or malformed contents. Recovery records and retained exception details still belong in private control; this is not a general-purpose log-redaction guarantee.

The synced files do not authenticate the scheduler, prove the directory survives machine loss, delete a provider secret, or fence another host. The real principal, storage, scheduler binding, recovery procedure and revocation lifecycle remain unqualified. A claim may exist even when no provider write occurred. Treat that outcome as ambiguous until authorized recovery verifies it; no automatic retry or release is implied.

An unresolved intent pins its original adapter implementation. Recovery after an upgrade requires the exact admitted implementation and dependencies; replacing the intent's fingerprint or generating unrelated keys does not recover the original write. Retained, verified old-build execution and an authorized migration procedure are not yet qualified.

The synthetic arithmetic cases are conformance fixtures, not private held-out benchmarks or evidence of agent competence. Preventing all evaluator detection is not guaranteed: filesystem/resource differences can be observed. Qualification omits evaluator-only markers, includes rotated inputs and an adversarial marker/tool-shadow test, and retains this limitation. Scientific benchmark validity and corpus contamination require #64/#246 protocols and independent evidence.

## Reproduce

```sh
python -m pytest -q tests/test_repository_evaluator.py tests/test_evaluator.py tests/test_work_contracts.py
python scripts/export_repository_evaluation_schemas.py --output-dir contracts/repository-evaluation
```

The required hosted job runs `bash scripts/qualify_repository_evaluator.sh` on an ephemeral GitHub runner, installs the exact verified vendor tool, and tests actual public/sealed evaluation, read-only candidate access, absence of control/sealed material and credentials, denied external network, timeout/output caps, and forced teardown. It fails if Docker/gVisor is unavailable. This is CI qualification; it does not install Fleet infrastructure or promote a service.

Primary upstream references: [gVisor installation](https://gvisor.dev/docs/user_guide/install/), [Docker integration](https://gvisor.dev/docs/user_guide/quick_start/docker/), [network controls](https://gvisor.dev/docs/user_guide/networking/), and [OCI runtime](https://gvisor.dev/docs/user_guide/quick_start/oci/).
