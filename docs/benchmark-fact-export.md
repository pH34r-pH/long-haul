# Offline benchmark fact export

Long Haul can export its append-only `benchmarks.jsonl` records as a versioned
`long-haul-benchmark-fact/v1` JSON document for a private derived research index.
The export command reads local files only; it has no database, Azure, credential,
or network dependency. Long Haul's benchmark store and scheduler remain the
sources of truth for observations and comparison eligibility.

```sh
python -m long_haul export-benchmark-facts \
  --input benchmark-matrix/benchmarks.jsonl \
  --output benchmark-facts.json

python -m long_haul export-benchmark-facts \
  --input benchmark-matrix/benchmarks.jsonl \
  --output benchmark-comparison.json \
  --reference-id 5de44208-339d-58ab-8dc7-f6cbf43f4159 \
  --not-before 2026-09-01T00:00:00Z \
  --as-of 2026-10-01T00:00:00Z
```

The export includes the complete `ModelArtifact`, profile JSON and canonical
profile identity, artifact key, complete `RuntimeIdentity`, execution mode,
ordered resource placement, exact workload name/token counts/cold state, source
provenance, error and known measurements. Metric units are explicit: seconds,
tokens per second, MiB and watts. Unspecified legacy utilization/network units
remain `unknown`; textual network fields are marked `not_applicable`. Prompt and
generated token counts describe workload shape and are not provider-billed
usage. Export/import validates the declared profile and artifact identities
before rebuilding an observation.

For native `llama-bench` input, keep one opaque ID per independent acquisition
and reuse it when replaying that exact acquisition. Pass source or importer
commits only when the full Git object IDs are known:

```sh
python -m long_haul import-llama-bench \
  --manifest matrix.yaml \
  --profile-id anc-g0 \
  --raw native-output.json \
  --output imported-matrix \
  --source-id llama-bench-acquisition-01 \
  --measured-at 2026-09-30T12:00:00Z \
  --source-repository example/producer \
  --source-commit 0123456789abcdef0123456789abcdef01234567 \
  --importer-commit 89abcdef0123456789abcdef0123456789abcdef
```

## Time and provenance

`measured_at` is the scientific measurement time. `recorded_at` is the existing
observation timestamp. For direct matrix runs those timestamps refer to the
measurement. Native `llama-bench` JSON does not itself provide a trustworthy
measurement timestamp, so an import without `--measured-at` emits
`measured_at: null`, retains the import time separately, and sets provenance to
`imported`. Existing `query_compatible` policy excludes that record from
performance reuse. Supplying a timezone-aware `--measured-at` preserves the
measurement time and allows the existing measured-provenance gate to evaluate
it. A failed run remains in the export with its error and cannot qualify.

Native imports retain the exact raw-file SHA-256 and zero-based row occurrence.
Their deterministic observation ID binds an opaque, path-free `source_id` to
that digest and row index. Reusing a source ID with the same bytes replays
idempotently; using the same explicit ID for different bytes fails visibly.
Give each independent acquisition its own ID, even when every metric is equal,
and reuse that ID when replaying the same acquisition. If omitted, the importer
derives a path-free ID from the input basename and digest; callers with two
acquisitions that could share that name and content must supply distinct IDs.
The importer never exports an absolute input path.

`source_commit` and `importer_commit` are separate optional fields and stay
unknown unless their exact values are provided. `RuntimeIdentity.build_id`
remains part of the runtime identity; it is not copied into either commit field.
Similarly, WorkContract, execution request, Compiler attempt and
`traceparent` correlation fields stay null unless Long Haul already knows the
exact ID. Matrix-run request IDs are retained from the actual
`ExecutionRequest`; no join is inferred from a model name, timestamp or trace.

An optional `traceparent` uses the W3C Trace Context version 00 field format.
It is operational correlation only and never scientific identity, attempt
identity or acceptance evidence. The existing `RuntimeIdentity` is emitted
verbatim. It identifies an inference engine such as `llama.cpp`, not a host
language runtime, so this exporter does not relabel it as OpenTelemetry's
`system.runtime.*` attributes or map it into development-stage GenAI semantic
conventions without a source-owned mapping.

## Comparability snapshot

When `--reference-id` is supplied, the export adds a comparison snapshot using
the selected observation's full profile, exact runtime, workload and execution
mode plus the explicit timezone-aware freshness cutoff. The `query_compatible`
flag is computed by the same predicates as `BenchmarkStore.query_compatible`;
each excluded record has stable reason codes such as `runtime_unbound`,
`resource_placement_mismatch`, `workload_mismatch`, `stale`, `run_failed` or
`provenance_not_measured`. This is a source-side eligibility prefilter, not a
ranking, aggregation, statistical qualification or winner policy. Preserve all
facts and reasons in the derived index and let Long Haul's existing scheduler
gate decide whether evidence can influence a candidate.

[`tests/fixtures/benchmark-comparison-summary.json`](../tests/fixtures/benchmark-comparison-summary.json)
is a synthetic comparison fixture with both compatible repeated measurements
and an unbound legacy row. It contains no hardware observation or private host
identifier.

Standards boundary: optional operational context follows the
[W3C Trace Context `traceparent` format](https://www.w3.org/TR/trace-context/).
No database schema, ingestion service, OpenTelemetry GenAI contract or new
storage authority is introduced here; private ingestion consumes the separate
Fleet-owned shared foundation after review.
