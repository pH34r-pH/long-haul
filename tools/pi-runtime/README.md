# Pinned Pi qualification assets

`package-lock.json` is the exact public npm resolution for
`@earendil-works/pi-coding-agent@1.5.2`. The upstream revision is
`earendil-works/pi@b4afdddf44e059c67ccf5097e5317d913521b470`.

The retained provenance summary records the earlier maintained npm 11.9.0
verification: 123 registry signatures and 38 attestations passed. The package
subject, upstream revision and tarball SRI were checked after verification.
Decoding attestation JSON alone is not signature verification. The installed
runtime and its credentials are not committed.

The hosted acquisition script repeats `npm ci --ignore-scripts` and
`npm audit signatures` in a clean configuration, checks the exact signed package
subject/source identity, and emits a content-addressed acquisition receipt.
`PiRuntimeAssets` rechecks the installed tree before execution. The official
Node image is separately pinned by Linux/amd64 OCI manifest digest in
`pi_worker.py`; it is not authenticated by the npm provenance claim.

The public deterministic local provider qualifies real Pi RPC/file-tool
mechanics in gVisor. It is not an LLM benchmark or a private held-out task.
