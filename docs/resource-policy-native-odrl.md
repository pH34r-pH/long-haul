# Native ODRL policy projection

`long_haul.resource_policy_odrl.export_native_odrl` projects reviewed resource
policy rules to an ODRL 2.2 Set. The caller supplies the independently
reviewed resource policy fingerprint. `verify_native_odrl` regenerates the
canonical graph from that source and rejects a different byte sequence.
Both operations use a fixed inline context and local data.
[ODRL Information Model](https://www.w3.org/TR/odrl-model/) · [ODRL Vocabulary and Expression](https://www.w3.org/TR/odrl-vocab/)

The versioned Long-Haul ODRL profile expands each rule into atomic actions
and principal assignments. ODRL constraints express inclusive date
limits, single values, and alternatives. The action IRIs are distinct
extensions of ODRL `use`; they preserve Long-Haul's separate read, index,
search, evaluation, training, and publication actions. The Set uses
ODRL `invalid` conflict handling. Permission duties carry their original
Long-Haul consent and review requirements. Prohibition duty declarations
appear as Long-Haul metadata because the runtime records them as facts.
The graph carries the source fingerprint, resource lifecycle,
retention and deletion facts, and lineage references.

## Context and qualified use

Long-Haul operands use the following authenticated context.

| Operand | Value |
| --- | --- |
| `purpose`, `crew`, `mission`, `contract`, `repository`, `audience` | Exact admitted request values |
| `purposeState` | `known-absent` only when authenticated control explicitly establishes that the request has no purpose |
| `admittedPolicy` | The reviewed policy fingerprint, available only after trusted control verifies the request's identity, lifecycle, retention, privacy, lineage, and any unresolved applicable prohibition |

The consumer must evaluate every required constraint and permission duty.
An unknown operand can leave a prohibition unresolved and must withhold
authorization until the consumer can establish the required context.
A purpose-free permission requires confirmed absence. A purpose-free
prohibition applies across purposes. A source digest or graph field cannot
supply the authenticated operand. The producer's rule projection and SHACL
tests qualify the exchange format. Independent runtime qualification
requires a consumer with this profile and trusted context admission.

## Reproduce the checks

Install the development dependencies and
`contracts/resource-policy/test-requirements.txt`. Run the native RDF and
SHACL tests without network access.

```sh
python -m pytest -q tests/test_resource_policy_odrl.py
```
