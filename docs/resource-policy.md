# Long-Haul Resource Policy Profile

Long-Haul needs one interoperable answer to two different questions:

1. **What is this resource, where did it come from, and what constraints apply?**
2. **May this authenticated caller perform this action for this purpose now?**

The resource profile answers the first question and supplies policy facts for the second. It does not replace caller authentication, Cedar authorization, `WorkContract`, MCP enforcement, evaluator evidence, or human review.

## Application-profile pattern

The profile follows the W3C Profiles Vocabulary pattern. It composes existing standards rather than creating a new general-purpose ontology:

| Dimension | Standard projection |
|---|---|
| Application-profile identity and resources | W3C PROF and `dct:conformsTo` |
| Permission, prohibition, duty, constraint | W3C ODRL 2.2 |
| AI-specific actions | Version-pinned ODRL AI Vocabulary |
| Source and transformation lineage | W3C PROV-O |
| Catalog/resource/distribution metadata | DCAT 3 and Dublin Core |
| Privacy, purpose, processing, consent, retention | Version-pinned DPV 2.0 profile |
| Quality measurements and certificates | W3C DQV |
| Dataset/corpus packaging | MLCommons Croissant 1.1 |
| Software/model/dataset BOM and license facts | SPDX 3.0.1 |
| Signed processing receipts | in-toto attestations |

ODRL AI Vocabulary and DPV 2.0 are Community Group outputs, not W3C Recommendations. Their exact versions must remain pinned and visible in evidence.

The reviewed ODRL AI working-draft source is pinned at commit `a4da7f9b42b2c5b02eff2aa644d9180b6e272ad2`; the [source manifest](../contracts/resource-policy/odrl-ai-source-pin.json) records the exact upstream paths and SHA-256 hashes. Run `python scripts/verify_odrl_ai_source.py --source-dir <download-directory>` against the two original upstream files to verify the pin without contacting the network. The pinned HTML describes the actions, but its accompanying Turtle file is a TODO skeleton. The pin is evidence of the source reviewed, not an executable ontology or a qualified interchange mapping. Upgrades require reviewing the source and action hierarchy again.

To obtain the exact source once and then verify it offline:

```sh
mkdir -p /tmp/long-haul-odrl-ai
curl -fsSL https://raw.githubusercontent.com/w3c/odrl/a4da7f9b42b2c5b02eff2aa644d9180b6e272ad2/ai-vocab/index.html -o /tmp/long-haul-odrl-ai/index.html
curl -fsSL https://raw.githubusercontent.com/w3c/odrl/a4da7f9b42b2c5b02eff2aa644d9180b6e272ad2/ai-vocab/ai-vocab.ttl -o /tmp/long-haul-odrl-ai/ai-vocab.ttl
python scripts/verify_odrl_ai_source.py --source-dir /tmp/long-haul-odrl-ai
```

Runtime code uses the Pydantic projection in `long_haul.resource_policy`; no RDF store is required. `profile_json_schema()` exports the ordinary JSON/YAML authoring schema. The current JSON-LD export and top-level SHACL assets are preliminary: nested policy semantics, lossless import, executable SHACL validation, and Croissant/SPDX package exports remain open under #248. They are not qualified interchange or dataset/BOM outputs.

The preliminary SHACL shape requires an explicitly typed ODRL policy at the linked policy URI. The current exporter does not produce that policy graph and therefore fails this check. Passing the shape alone does not establish that permissions, constraints, prohibitions, or other policy semantics are complete or safe to enforce.

Install `contracts/resource-policy/test-requirements.txt` alongside the development dependencies to run the policy conformance tests locally.

## Local authority classes

External rights and provenance vocabularies do not determine whether Long-Haul may interpret content as institutional control. The profile therefore adds a deliberately small operational class:

- `authoritative-control`
- `reviewed-task`
- `accepted-memory`
- `repository-reference`
- `external-source`
- `generated-content`
- `evaluator-evidence`
- `unknown`

The class is derived from authenticated source, provenance, policy, and review. Content cannot self-promote by containing profile-shaped text. A README, memory, issue, tool result, generated document, or imported JSON-LD remains data unless the control plane independently admits its authority.

## Actions are independent

The evaluator distinguishes:

- read
- index
- search
- AI/model context augmentation
- evaluation
- data mining
- ordinary AI training
- generative-AI training
- derivation and annotation
- distribution and publication
- retention and deletion

Permission to perform one action never implies another. In particular:

- indexing does not imply model augmentation;
- model augmentation does not imply training;
- evaluation does not imply training;
- training does not imply publication;
- public accessibility does not imply training permission.

Consequential actions require an explicit purpose. Missing policy produces `UNKNOWN`, which enforcement treats as fail-closed.

## Evaluation semantics

`ResourcePolicyEvaluator` evaluates the resource-side policy deterministically against a normalized request containing authenticated caller and Long-Haul context. It returns:

- `ALLOW`
- `DENY`
- `REQUIRE_CONSENT`
- `REQUIRE_REVIEW`
- `UNKNOWN`
- `CONFLICT`
- `EXPIRED`
- `RETRACTED`
- `UNAVAILABLE`

The result is an input to Long-Haul authorization. The caller still needs ordinary capability, mission, contract, sandbox, resource, and institutional authorization.

Rules:

1. Explicit prohibitions and incompatible lifecycle state fail closed.
2. Matching permission and prohibition is a conflict, not implicit precedence.
3. Missing permission is unknown, never allow.
4. Unsatisfied duties require consent or review.
5. Stale or superseded resources require review.
6. Retraction, deletion, expiry, and unavailable state cannot be optimized away.
7. A derived resource use is allowed only when every relevant source policy allows the same action and purpose.
8. Redaction or anonymization creates a new derived resource with its own identity and evidence; it does not rewrite the source.

Decisions bind the complete authenticated request fingerprint. Derived decisions require identical action, purpose, caller, context, and time across sources and retain each source policy fingerprint. Unscoped prohibitions apply to every purpose; privacy purpose, invalidation, deletion-required, and retention boundaries cannot be overridden by a permission. Explicit deletion permission permits cleanup of expired or invalidated resources without granting other use.

Policy profiles, caller identity, fulfilled-duty evidence, and time are admitted by authenticated control code. Loading a profile-shaped object from repository text does not authenticate it. Consumers must compare exact reviewed policy fingerprints before invoking this evaluator; its result does not authenticate its input issuer or assert legal validity.

## Enforcement boundary

The intended control flow is:

```text
resource profile + requested action/purpose
    -> deterministic resource-policy decision
    -> normalized policy facts
    -> Cedar / Long-Haul caller-aware authorization
    -> MCP or execution-fabric invocation check
    -> provenance-bearing decision and outcome events
```

Do not implement separate ODRL/DPV interpretations in each tool or adapter. Repository context, Nacre Recall, trajectory corpora, publication, and private-data products consume the same normalized evaluator.

## Initial consumers

- repository context manifests and sealed evaluator inputs;
- Nacre Recall sources, memories, recall envelopes, and capture proposals;
- code-agent trajectory and training corpora;
- Knowledge, Tone, DSL, and Experiment Compiler packages;
- Personal Algorithm, Family Tree, Wardriver, and Society private data;
- multimodal application/device evidence.

## Validation

Run the ordinary test suite and export the authoring artifacts:

```bash
python -m pytest tests/test_resource_policy.py
python scripts/export_resource_policy_profile.py
```

The exporter performs no network resolution. External standards identifiers are metadata and profile dependencies, not mutable runtime imports.
