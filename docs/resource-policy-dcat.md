# Published dataset distributions

`long_haul.resource_policy_dcat.export_dcat_distribution` describes one
published distribution of a versioned dataset in [DCAT 3][dcat]. It uses the
publication's direct download URI, exact size, SHA-256, media type, issue time,
and public receipt URI. Public access, the license, other rights statements, and
an ODRL policy link are separate RDF properties. Include the optional rights
references only when they are public and match the reviewed resource policy.

Give the exporter a `PublishedDistribution` and the matching distribution
policy. The publication descriptor's fingerprint covers the entire reviewed
record, including the receipt digest. Supply the expected publication and
policy fingerprints from trusted review of the exact bytes, the receipt, and the
metadata approved for public catalog use. The exporter is offline. The caller
checks the remote receipt and archive when admitting or refreshing those pins.

The request must name the distribution and specify the `derive` action,
`dcat-distribution-metadata` purpose, and `public` audience. A derived or
redacted distribution also needs the exact transitive source policies and their
independently reviewed fingerprints. All source policies must permit that use.
Keep the source identities and the decision receipt in the trusted review
context. The JSON-LD contains the public distribution and receipt references.
Publication and redistribution remain distinct resource policy actions.

The inline context can expand offline. The
[local SHACL shapes](../contracts/resource-policy/dcat-distribution.shacl.ttl)
check the supported dataset, distribution, and checksum subset. Use the
reviewed fingerprints and the evaluator for admission. SHACL checks the RDF
structure, while the reviewed publication evidence binds the actual bytes.

[dcat]: https://www.w3.org/TR/2024/REC-vocab-dcat-3-20240822/
