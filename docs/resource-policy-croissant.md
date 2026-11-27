# Croissant dataset metadata

[The resource policy projector](../src/long_haul/resource_policy_croissant.py)
exports local Croissant 1.1 metadata for an independently reviewed,
already published dataset edition. It preserves the dataset identifier,
version, publication date, creators, license references, and each file's
name, media type, location, and SHA-256. The caller supplies the actual
publication date and any citation. The local exporter makes no requests
to dataset hosts. [MLCommons Croissant 1.1 specification](https://docs.mlcommons.org/croissant/docs/croissant-spec-1.1.html)

`DatasetRelease` describes the published metadata and file inventory. Its
fingerprint is the SHA-256 of the canonical JSON projection of those facts,
including the ordered file list and each file hash. The dataset's
`ResourcePolicyProfile.identity.content_digest` must contain that exact
fingerprint. The caller supplies the reviewed policy fingerprint from
independent admission and a `ResourceUseRequest` for `derive` with purpose
`croissant-metadata`. Use the existing authenticated application context
and its current evaluation time when constructing the request.

`export_croissant` returns canonical JSON-LD bytes and the existing
`PolicyDecision`. For derived or redacted datasets, pass the complete
ancestor policy closure and its independently reviewed fingerprints.
Every policy in that closure must permit the same metadata derivation.
The combined decision records the policy fingerprints. A manifest
mismatch, missing source, cycle, or blocked policy stops the export.

The result describes the dataset's published file inventory. A caller
still needs the applicable use decision before reading files, training,
distributing data, or publishing this metadata. Validation of file bytes,
frozen partitions, decontamination, and evaluator evidence is part of
the governed corpus release under [Long Haul #246](https://github.com/pH34r-pH/long-haul/issues/246).

The synthetic fixture in `tests/test_resource_policy_croissant.py` exercises
exact identities and purpose, changed policy and inventory, expired or
retracted data, and incomplete or incompatible source policy. The generated
metadata can be checked with `mlcroissant validate --jsonld croissant.json`.
