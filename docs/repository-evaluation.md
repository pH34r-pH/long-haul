# Frozen repository evaluation

Repository evaluation composes the existing WorkContract, EvidencePacket and event history. It introduces no scheduler, database, agent framework, or production authority. Ordinary executable/artifact predicates and historical schema-v1 evidence remain readable.

## Authority and identity

`RepositoryEvaluationProfileV1` is authenticated control-plane input. It pins the task, evaluator implementation, sandbox implementation, immutable OCI image, exact gVisor bundle, resource/network policy, and every check. Repository or model text cannot supply this admission.

The observed evaluator implementation reference includes its relevant source files and the Python, Pydantic and Pydantic Core version strings. This reference is not an attestation of loaded code or host integrity. Adding Pydantic Core to the reference changes the identity: existing pinned evaluator profiles and dependent private key references must be regenerated and readmitted before use; historical evidence remains historical.

`AcceptancePredicate.evaluator_reference` explicitly identifies a profile-owned check without putting sealed commands into WorkContract. It cannot coexist with an executable/artifact definition. The ordinary evaluator reports these predicates as unknown; only `RepositoryEvaluationRunner` resolves them under the exact profile references in the repository extension. This leaves ordinary command meanings unchanged.

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

The synthetic arithmetic cases are conformance fixtures, not private held-out benchmarks or evidence of agent competence. Preventing all evaluator detection is not guaranteed: filesystem/resource differences can be observed. Qualification omits evaluator-only markers, includes rotated inputs and an adversarial marker/tool-shadow test, and retains this limitation. Scientific benchmark validity and corpus contamination require #64/#246 protocols and independent evidence.

## Reproduce

```sh
python -m pytest -q tests/test_repository_evaluator.py tests/test_evaluator.py tests/test_work_contracts.py
python scripts/export_repository_evaluation_schemas.py --output-dir contracts/repository-evaluation
```

The required hosted job runs `bash scripts/qualify_repository_evaluator.sh` on an ephemeral GitHub runner, installs the exact verified vendor tool, and tests actual public/sealed evaluation, read-only candidate access, absence of control/sealed material and credentials, denied external network, timeout/output caps, and forced teardown. It fails if Docker/gVisor is unavailable. This is CI qualification; it does not install Fleet infrastructure or promote a service.

Primary upstream references: [gVisor installation](https://gvisor.dev/docs/user_guide/install/), [Docker integration](https://gvisor.dev/docs/user_guide/quick_start/docker/), [network controls](https://gvisor.dev/docs/user_guide/networking/), and [OCI runtime](https://gvisor.dev/docs/user_guide/quick_start/oci/).
