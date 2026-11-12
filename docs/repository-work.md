# Repository work and bounded context

`WorkContract.repository_work` is an optional `RepositoryWorkExtensionV1`. Ordinary contracts and historical evidence remain readable. WorkContract continues to own objective, writable scope, capabilities, authority, budgets, and acceptance. The extension supplies exact repository, task, environment, toolchain, dependency/cache/network, context, and evaluator references; it contains no source payloads or credentials.

## Exact source and retrieval

`git_repository_identity` captures an exact commit/tree, all gitlinks, and caller-declared generated/LFS material identities. `GitContextProvider` reads immutable Git blobs, independently of the checkout. It rejects links and submodules as file content, never invokes filters or checkout hooks, disables replacement objects and implicit network acquisition, and limits individual blobs to 1 MiB. LFS pointers remain pointers; the provider neither downloads nor claims to have inspected their target content.

`select_repository_context` consumes the provider-neutral `RepositoryContextProvider` interface. Its initial maintained implementations use Git object plumbing and Python's standard-library AST:

| Lookup | Implementation | Unavailable behavior |
| --- | --- | --- |
| Range | Frozen UTF-8 file lines | Missing/invalid source is explicit |
| Lexical | Literal line search | No-match is explicit |
| Syntax | Python AST node type | Unsupported language or invalid syntax is unavailable |
| Symbol | Python AST definition/name locations | No fabricated LSP/SCIP/index results |

LSP, Tree-sitter, and SCIP can supply future providers through the same seam. The first implementation needs no index service, embeddings, model, GPU, or network. A caller can retry lexical/range lookup after an unavailable syntax/index capability; both attempts remain in the manifest.

## Policy and instruction boundary

The caller supplies an authenticated `ContextAdmission` registry outside the repository. Each entry pins the shared resource-policy fingerprint, exact content digest, source/authority/data classification, and complete use request. Repository text is never parsed into this registry. Unknown policy, changed policy, cross-repository or cross-session use, incompatible privacy, and unadmitted external/private data fail closed.

All model-facing context requires explicit `AI_AUGMENT` permission. Lexical selection additionally requires `SEARCH`; permission to read alone cannot place a source into a model prompt. The shared evaluator decides resource-side permission. The caller still enforces capabilities and institutional authorization. A private source cannot be relabelled public through this provider; a reviewed redacted derivative needs separate identity and evidence.

The manifest records source slices, full-content and excerpt digests, exact request/policy fingerprints, the observed policy evaluator implementation identity, source class, retrieval implementation/version/configuration, query/reason, creation provenance, and omissions. For lexical selection both required decisions must identify the same evaluator. This identity is a source/runtime reference, not host or loaded-code attestation. The manifest records selection rather than correctness or sufficiency. Imperative README, code, issue, PR, or generated text remains evidence and cannot change contract authority or acceptance.

## Budgets, verification, and resume

`max_bytes` bounds excerpt UTF-8 bytes; `max_items` bounds selected slices. `max_delivery_bytes` independently bounds the serialized worker context, including metadata. At most 128 requested lookups and 256 omission records are admitted. A token ceiling without an exact tokenizer fails explicitly as `token_budget_unavailable`; no token estimate is presented as a measurement.

`verify_repository_context` independently reconstructs selection from the frozen provider and currently admitted policy registry. Changed source, materials, retrieval or policy evaluator implementation, policy, or use identity invalidates the prior manifest. Historical items without a policy evaluator identity remain readable with their original serialization and fingerprint, but must be reselected before verification or delivery. `RepositoryContextManifestV1.assert_bound` binds it to the repository extension's exact source/task/environment/context identities.

`repository_context_event` emits only a compact context reference and omission/truncation counts into existing events. Checkpoints and TaskDigest rehydrate those references without transcript payloads. The digest retains machine-readable context omissions even when its text budget cannot display them. Pi and fixture harnesses share the same evidence renderer; Pi delivery additionally requires the exact WorkSession and contract/mission binding, current authenticated admission registry, and current control-plane time. Permission is re-evaluated at delivery so an exact historical view cannot bypass expiry or revocation. The raw renderer is a data projection and grants no model-use permission. The worker sees stable source/task/environment/context references without sealed evaluator material.

## Schemas and checks

Export offline JSON Schemas with:

```sh
python scripts/export_repository_work_schemas.py --output-dir contracts/repository-work
python -m pytest -q tests/test_repository_context.py tests/test_work_contracts.py tests/test_checkpoints.py tests/test_pi_rpc.py
```

The schemas are checked in for exact-commit downstream pins and tested against runtime export. Consumers must validate semantics and evidence as well as schema shape. No source/package checks here claim sandbox security, real Pi execution, sealed evaluator qualification, deployment, or device qualification; those remain #243–#245/#64 gates.

Primary implementation references: [Git object inspection](https://git-scm.com/docs/git-cat-file), [Git tree inspection](https://git-scm.com/docs/git-ls-tree), and [Python AST locations](https://docs.python.org/3.12/library/ast.html).
