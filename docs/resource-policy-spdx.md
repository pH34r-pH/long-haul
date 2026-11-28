# SPDX package inventory export

Long-Haul can project a reviewed inventory of software, model, and dataset
packages into an SPDX 3.0.1 JSON-LD document. Each package keeps its
SHA-256, version, declared and concluded licensing, and explicit dependency
links. Dataset packages also carry the recorded build time, download
location, and dataset types. The document uses the SPDX Software, AI,
Dataset, and licensing vocabulary where applicable. [SPDX 3.0.1 specification](https://spdx.github.io/spdx-spec/v3.0.1/) · [Dataset profile](https://spdx.github.io/spdx-spec/v3.0.1/model/Dataset/Dataset/) · [Licensing profile](https://spdx.github.io/spdx-spec/v3.0.1/model/Licensing/Licensing/)

Install the optional `spdx` extra and use
`long_haul.resource_policy_spdx.export_spdx_bom`. The caller provides a
`SpdxBomManifest`, its independently reviewed SHA-256, and the exact
resource policies and independently reviewed fingerprints for the packages
and their full derivation and redaction ancestry. The manifest includes
the SPDX creator and creation time, a root package, and the facts for
each package in the root's dependency graph.

The request uses action `derive`, purpose `spdx-bom`, and the root package
resource ID. Supply the application’s authenticated caller context and
current policy evaluation time. Every involved resource must allow
that use. Package kind and SHA-256 must match the reviewed resource identity.
The exporter returns deterministic UTF-8 JSON-LD bytes and the existing
`PolicyDecision` with the evaluated source policy fingerprints.

Supply the declared license from the artifact and the concluded license from
the independent review. Keep a conclusion comment for any difference.
The pinned license parser validates public SPDX expressions. `NONE` and
`NOASSERTION` are explicit facts and point to the SPDX named individuals.
A license reference does not confer permission to distribute, train, or
publish. The exporter operates on metadata without fetching URLs,
opening packages, or performing a license analysis.
[SPDX license expressions](https://spdx.github.io/spdx-spec/v3.0.1/annexes/spdx-license-expressions/)

Run policy and projection tests with the `dev` extra.

```sh
python -m pytest -q tests/test_resource_policy_spdx.py
```

For offline standards conformance, install `jsonschema==4.25.1` and the
repository's resource-policy test requirements. Place the official
`spdx-json-schema.json`, `spdx-context.jsonld`, and `spdx-model.ttl` files
in an independent directory. The test checks their pinned SHA-256
values before validating the synthetic mixed-package BOM. It loads the
official context locally when expanding the required SPDX context URL.
[JSON Schema](https://spdx.org/schema/3.0.1/spdx-json-schema.json) · [Context](https://spdx.org/rdf/3.0.1/spdx-context.jsonld) · [OWL and SHACL](https://spdx.org/rdf/3.0.1/spdx-model.ttl)

```sh
LONG_HAUL_SPDX_301_ASSETS=/path/to/verified/spdx-3.0.1 \
  python -m pytest -q tests/test_resource_policy_spdx.py
```
