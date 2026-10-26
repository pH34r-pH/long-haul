# Experimental reason-maintenance slice

Status: **experimental standalone prototype**, September 2026; tracks #139 and
follows #131 and [the north star](north-star.md). This is not a production kernel
or an integration with #138's epistemic materializer. Issue #139 remains open.

## Scope and semantic contract

`src/long_haul/reason_maintenance/` contains an optional CLIPS adapter and a
backend-neutral corpus runner. The normative, synthetic corpus is
`tests/fixtures/reason_maintenance/corpus-v1.json`. Its expectations are declared
independently of the adapter; there is no new Python ATMS/JTMS or reference TMS.
Future native and external oracle adapters can implement the same `Backend`
protocol and run the same cases.

An immutable version-1 `Program` declares stable signed assertion identities and
acyclic, nonempty conjunctive justifications. Every support and justification
activation has exact event provenance. Evidence roots require an artifact/source
reference; assumptions are separate retractable roots. Withdrawal removes only
that support. Multiple roots and multiple enabled justifications can support the
same assertion independently. Disabling a justification retracts its support;
re-enabling it records the new activation event. Support IDs cannot be reused.

CLIPS `logical` conditional elements own derivation support and automatic
retraction. A claim is deduplicated by its signed assertion identity. Separate
logical derivation facts report which justifications are active; explanations
map those facts to stable justification/premise identities and transitive active
roots. The Python explanation walk does not infer new claims. It preserves all
active roots and does not claim to compute minimal proofs or inconsistent sets.

Positive and explicit negative assertions are independent. Status is derived
only from their presence:

| Positive supported | Negative supported | Status |
| --- | --- | --- |
| yes | no | `SUPPORTED_ONLY` |
| no | yes | `REFUTED_ONLY` |
| yes | yes | `BOTH` |
| no | no | `NEITHER` |

Missing support is unknown, not inferred negation. A contradiction does not add
any unrelated claim or disable either polarity. Ordinary rules can explicitly
consume either polarity. There is no implicit conflict resolution, contraposition,
classical explosion, closed-world negation, preference, or defeasible rule system.

## Replay and identity boundary

The experiment consumes existing `Event` envelopes (`observation`, envelope
schema version 1) with a versioned `reason_maintenance_experiment` payload.
Unrelated events are ignored; malformed experimental payloads, unsupported
versions/envelopes, and duplicate event identities are rejected. These fixture
operations are **not** a new canonical event vocabulary or #138 integration API.
The ordered event history and the immutable versioned program are both required
replay inputs. Program declarations contain no live support state.

The append-only event store is read unchanged. Backends can be discarded and
rebuilt; snapshots/explanations contain domain assertion, support, justification,
artifact, and authoritative event identities only. Internal symbols are hashed
private tokens, so domain IDs are never interpolated as CLIPS syntax. Fact
handles/indexes and rules are disposable; they are never persisted. There are no
changes to event-store schemas, WorkContract, crew, checkpoints, scheduler,
inference transport, or production runtime entrypoints.

#138 integration is deferred to a separately reviewed mapping from its semantic
materialization. This corpus consumes its own stable fixture assertion IDs and
has no dependency on `long_haul.epistemic` runtime APIs.

## Exact dependency and compatibility evidence

Install the standalone prototype from the mature PyPI registry:

```sh
python -m pip install -e '.[dev]' 'clipspy==1.0.6'
LONG_HAUL_REQUIRE_CLIPS=1 python -m pytest -q tests/test_reason_maintenance.py
```

Shared dependency manifests are deliberately unchanged. If a coordinated optional
extra is wanted later, the exact proposed addition under
`[project.optional-dependencies]` is:

```toml
reason-maintenance-experiment = ["clipspy==1.0.6"]
```

[CLIPSpy 1.0.6's tagged build recipe](https://github.com/noxdafox/clipspy/blob/1.0.6/Makefile)
uses the official `clips_core_source_642.zip` archive for CLIPS 6.4.2. The evaluated
CPython 3.12 Linux x86-64 wheel identifies its embedded core as
`CLIPS (6.4.2 1/14/25)`. The adapter requires **both CLIPSpy 1.0.6 and CLIPS 6.4.2**;
it rejects different or unidentified builds instead of substituting another core.
CLIPSpy exposes no core-version query, so this conservative check reads the
vendor banner in the native extension. This is build identification, not a
general ABI certification; custom builds lacking the banner need separate review.

The [official CLIPS documentation](https://clipsrules.net/) includes the 6.4.2
Basic and Advanced Programming Guides. The implementation uses logical
conditional elements and ordinary facts/rules. CLIPS is public domain; CLIPSpy's
binding is BSD-3-Clause. No vendor source is copied into this repository.

## Conformance evidence and open gaps

The corpus covers evidence and assumptions, two independent roots, two
independent justifications, single-support retention, last-support and downstream
cascade, conjunction, justification disable/re-enable, independent positive and
negative support, every four-valued state, unrelated unknown preservation, and
active explanation/provenance. Tests compare incremental updates with clean
rebuilds and replay the stored JSONL history in two fresh Python processes,
requiring identical complete serialized snapshots and unchanged event bytes.
Additional checks reject malformed operations, invalid dependencies/identities,
cyclic programs, backend version mismatch, and syntax-bearing domain identities.

The dedicated experimental CI workflow installs the exact optional dependency
on Python 3.11 and 3.12 and fails if it cannot load; missing-backend skips in the
ordinary runtime suite do not certify conformance. Corpus versions and external
oracle results remain independent of the chosen first adapter.

Known gaps before promotion:

- Acyclic explicit signed Horn rules only; recursion and cycles are rejected.
- No Drools/KIE or LTMS oracle execution yet; hand-declared normative fixtures
  are the comparison baseline, not an independently executed second engine.
- No ATMS worlds, nogoods, minimal inconsistent sets, belief preferences,
  defeasible conflict resolution, or solver projections.
- No #138 runtime integration or new authoritative domain event schema.
- No native Rust adapter or generalized CLIPSpy/core version compatibility.
- ARM64/Kestrel build availability and resource behavior are not qualified by
  Linux x86-64 unit tests; no fallback version is accepted.
- **Reference-vessel and Kestrel resource footprints are unmeasured.** No physical
  execution or new benchmark was performed. Both measurements and separately
  reviewed integration are required before promotion beyond experimental status.
