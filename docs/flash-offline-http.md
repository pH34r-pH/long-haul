# Flash offline HTTP adapter

`long_haul.work.FlashHTTPAdapter` exposes the in-memory `FlashDeliveryFixture`
through a dependency-free WSGI interface for deterministic protocol tests. It
is synthetic and non-durable: process restart loses jobs, attempts, leases, and
accepted results. It does not qualify production browser admission or hardware.

The controller injects the F0 admission inputs when it constructs the adapter.
The HTTP caller cannot claim a capability or select a higher tier. Unknown
capability, missing memory evidence, and failed initialization decline through
the existing F0 policy. The checked-in toy verifier is test-only.

All routes require `POST`, `Content-Type: application/json`, and bounded JSON
object bodies. The routes are:

| Route | Request | Response |
| --- | --- | --- |
| `/v1/jobs/claim` | `{}` | `claimed`, `no_work`, or `declined` |
| `/v1/jobs/{job_id}/renew` | `{"lease_id":"..."}` | renewed expiry or lease conflict |
| `/v1/jobs/{job_id}/complete` | `{"lease_id":"...","result":...}` | completed result, invalid result, or lease conflict |
| `/v1/jobs/{job_id}/abandon` | `{"lease_id":"..."}` | abandoned or lease conflict |

Completion delegates to external fixture verification and rechecks the lease
and wall deadline after verification. A retry with the same accepted result is
idempotent even if the original response was lost; a different result cannot
rewrite the accepted result. This property is scoped to the live in-memory
process and is not a durable idempotency guarantee.

Owner operations that view work are separate from worker compute donation. No
browser admission, access grants, sensors, model execution, or production
queueing is provided here. A durable backend remains a separate qualification
gate: Azure Queue Storage plus a conditionally written result store must be
designed and validated on its own before any migration or production claim.
