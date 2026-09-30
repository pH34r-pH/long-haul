# Epistemic materialization v1 — bounded slice of #138

The append-only `EventStore` remains authoritative. `long_haul.epistemic` builds a
fresh in-memory RDF graph; it never appends events, changes checkpoints, assigns
backend identities, runs reasoning, or touches execution/scheduling. Install
`long-haul[epistemic]` for RDFLib, PyLD and pySHACL. The `dev` extra includes them
so both normal CI Python targets exercise this feature. Native versions are
selected by `uv.lock`; core runtime dependencies are unchanged.

```python
from long_haul.events.store import EventStore
from long_haul.epistemic import materialize, validate_graph, export_jsonld, import_jsonld

graph = materialize(EventStore("events.jsonl").events())
validate_graph(graph)
snapshot = export_jsonld(graph)
restored = import_jsonld(snapshot)
```

The public interchange contract is [RDF 1.1](https://www.w3.org/TR/rdf11-concepts/)
and [JSON-LD 1.1](https://www.w3.org/TR/json-ld11/), using
[PROV-O](https://www.w3.org/TR/prov-o/) for entities, generating/invalidating
activities and agents, and [SHACL](https://www.w3.org/TR/shacl/) for structure.
RDFLib supplies RDF serialization and SPARQL 1.1 queries; PyLD processes JSON-LD
with explicit 1.1 mode; pySHACL runs the packaged shapes. No service, database,
OWL inference or RDF 1.2 feature is required. A future backend can consume the
same standard triples and conformance corpus without owning log identity.

## Explicit adapter input

Only events carrying `payload.epistemic` opt in. Existing unmarked events keep
their existing behavior; no interpretation of arbitrary historical prose is
attempted. The marker is a narrow versioned adapter input within the existing
payload, not a replacement event schema or a canonical custom graph format.
Existing event types, event IDs and schema versions remain unchanged.

Each marker requires `version: 1` and a `kind`:

| Kind | Required fields | Optional fields |
| --- | --- | --- |
| `claim` | nonempty `text` | `supports`, `negates`, `artifacts` |
| `evidence` | nonempty `text` | `supports`, `artifacts` |
| `assumption` | nonempty `text` | `supports`, `artifacts` |
| `contradiction` | two distinct `targets` | none |
| `retraction` | one `targets` entry | none |

References are exact earlier annotated event IDs, not graph/backend IDs.
`supports` points from the supporting object to the supported object; `negates`
references an earlier claim. Contradiction targets must be claims or assumptions.
Retraction targets must be claims, evidence or assumptions. Lists reject empty
or duplicate entries. Unsupported annotation fields/versions, marked event schema
versions other than 1, naive timestamps, missing/duplicate event IDs and dangling
or forward references raise `ValueError`. Replay is in append order, not timestamp
order. Relation events cannot become support objects or retraction targets.

Example existing-type event payload:

```json
{"epistemic": {"version": 1, "kind": "evidence", "text": "Synthetic observation.", "supports": ["claim-event-id"], "artifacts": ["sha256:exact-fingerprint"]}}
```

The adapter copies only the allowlisted annotation and event provenance metadata.
It does not copy scratchpads, hidden reasoning, arbitrary tool payloads or full
prompts. Callers must supply concise public claims/evidence rather than private
reasoning in `text`; this adapter is not a content redactor.

## RDF projection semantics

The `urn:long-haul:epistemic:v1:terms:` vocabulary supplies only domain distinctions
missing from the chosen standards: Claim, Evidence, Assumption, Contradiction,
Retraction, Artifact, AuthoritativeEvent, Projection, event/version/fingerprint
properties and explicit supports/negates/contradicts/target relations. It is a
versioned projection mapping, not a new authority or truth calculus. Descriptions,
source and agent identity use DCTERMS. No evidence is inferred from text equality.

Projection IRIs are injective percent encodings of exact event, agent or artifact
strings in separate v1 namespaces. Every projected object has `prov:wasDerivedFrom`
an event entity retaining the exact `eventId`, type and schema version, and
`prov:wasGeneratedBy` an activity that `prov:used` that same event. Actor identity
is retained on a PROV agent; source and artifact fingerprint strings are preserved
without normalization. Artifacts are references, never fetched or authenticated.

Retraction records remain provenance-bearing objects. They add
`prov:wasInvalidatedBy` and the first append-order `prov:invalidatedAtTime` to the
target without deleting its description, support or event provenance. Repeated
retractions retain every invalidating activity and the initial timestamp.
Contradiction records retain both targets and symmetric explicit contradiction
links; negation is explicit, never inferred from sentence wording. No support
cascade, live status, consistency/entailment or contradiction resolution is
implemented here. Those concerns belong to #139 and later adapters.

`materialize` always allocates a new graph. Delete any serialized snapshot and
replay the same event history to obtain equivalent RDF triples. JSON-LD byte
order and blank-node labels are not a semantic identity guarantee; compare RDF
graph isomorphism. The fixture `expected.ttl` is a reviewable standard RDF snapshot.
Queries can use `Graph.query` with SPARQL 1.1.

## Validation and limits

Packaged SHACL shapes enforce version metadata, object descriptions, exact event
provenance links, activity linkage, target kinds/counts, artifact fingerprints and
agent identities. Export and import validate; callers can explicitly validate a
fresh materialization. Invalid graphs raise `InvalidProjection`. Validation proves
structure, not event authenticity, artifact contents or truth. Imported graphs
are disposable snapshots and cannot create authoritative events.

Import uses an offline document loader: remote contexts/documents are rejected.
Inline JSON-LD contexts and expanded RDF remain standard; named datasets are
outside this single-graph profile. Arbitrary SPARQL or externally supplied RDF
must be handled with the caller's resource/access limits; this package does not
expose a server or execute queries from events.

Synthetic fixtures cover claims, explicit negation, evidence, assumption,
contradiction, retraction, agents and exact event/artifact provenance. Tests cover
complete snapshot deletion/rebuild, unchanged JSONL source history, JSON-LD 1.1
round trips, standard Turtle equivalence, SPARQL queries, malformed SHACL data,
fail-closed marked inputs and offline import boundaries. They make no physical
measurement or acceptance claims.

#138 remains open: historical event adapters, preferences/priorities, subgoal/task
structure, broader support/justification structures and runtime integration are
future slices. #130's broader prior-art/adoption research also remains separate.
