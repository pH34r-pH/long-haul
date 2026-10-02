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
browser admission, access grants, sensors, model execution, live Azure
provisioning, or production endpoint wiring is provided here.

## Offline Azure storage adapter

`long_haul.work.DurableFlashDelivery` accepts injected Azure Queue Storage and
Blob Container SDK clients, plus an injected clock, verifier, and notifier. It
is tested offline with fakes and does not connect to Azure by itself. Queue
messages contain only a version and `job_id`; contract, deadline, attempt and
claim-generation counters, immutable accepted result, and notification state
live in one versioned per-job blob. State changes use ETag conditional writes.
Attempt or wall-budget exhaustion writes a terminal reason before the queue
reference is deleted, so duplicate triggers do not revive exhausted work.

Claim tokens are generated with a cryptographically secure random factory by
default. The service process keeps token-to-message and rotating pop-receipt
state in memory; only a hash of the claim token is stored in the job record.
Queue receipts are never returned by the adapter's worker claim result. A
process restart drops outstanding receipt mappings, so Queue visibility expiry
redelivers the reference and a new claim generation fences the prior worker.

Acceptance is committed to Blob state before the trigger message is deleted.
If a process stops between those services, redelivery observes the accepted
record and removes the trigger without executing the work again. Accepted
records whose output notification is pending are scanned and replayed before
each claim or by calling the recovery method. Queue and Blob do not share a
transaction: execution and notification delivery are at least once. An output
notifier must dedupe on the stable accepted attempt ID. Conditional state
updates choose one accepted generation/result. This adapter alone is not a
production gateway, qualification, rate limiter, or cross-service atomicity
guarantee.
