# Flash Vessel browser capability prototype

**Status: local prototype. This is not part of the Long Haul runtime or a hosted service.**

This prototype demonstrates the first page-owned Flash boundary from [Long Haul #155](https://github.com/pH34r-pH/long-haul/issues/155) and [#156](https://github.com/pH34r-pH/long-haul/issues/156): work starts only after an explicit **Join for this tab** action, cheap browser-exposed hints keep their unknown state, the instance ID exists only in worker memory, and the page can pause or stop the worker. It does not claim or execute work.

## Run locally

Serve this directory over localhost or HTTPS, then open `index.html`. Before joining, the page creates no worker. Joining creates one module Dedicated Worker and returns a single capability snapshot. Hiding the page marks the worker paused; showing it resumes the page-owned session. **Stop this tab's worker** sends a stop message and terminates the worker. A page-hide also terminates it. Rejoining starts a fresh session with a new random ID.

The prototype performs capability inspection only. It does not benchmark or stress the host, load a model, request sensors, contact a task API or telemetry service, store an identity, or contact a queue. WebGPU detection asks for an adapter and reads only browser-exposed features and selected limits. A model/tier policy must come from the private qualification work in [Domain Scaling Lab #580](https://github.com/pH34r-pH/domain-scaling-lab/issues/580); this prototype intentionally does not choose a tier.

## Snapshot contract

`capability-snapshot.d.ts` declares the version 1 shape. Every hint uses a tagged union:

```ts
type CapabilityHint<T> =
  | { status: "available"; value: T }
  | { status: "unavailable" }
  | { status: "unknown" };
```

An absent, withheld, malformed, or failed probe is `unknown`. `unavailable` means the browser explicitly reported no adapter/support. A returned value is only a browser hint: `hardwareConcurrency` is not a reservation, `deviceMemory` is coarse rather than free memory, and WebGPU limits do not disclose VRAM or exclusive GPU ownership. No hint authorizes a workload by itself.

The snapshot contains:

- session-only `flashInstanceId` and current page visibility;
- logical processor count and coarse device-memory hint when exposed;
- WebAssembly SIMD validation and SharedArrayBuffer availability;
- WebGPU adapter features and five numeric limits used as possible future admission inputs.

No browser fingerprint, adapter name, or stable vessel identity is collected. `navigator.storage.estimate()`, Compute Pressure, WebNN, and sensors are outside this first slice.

## Browser standards used

- [WHATWG HTML Web Workers](https://html.spec.whatwg.org/multipage/workers.html) defines the page-owned Dedicated Worker and message channel used here. The compute loop belongs in that worker, not the page's rendering thread.
- The [W3C Device Memory API](https://www.w3.org/TR/device-memory/) is a secure-context Working Draft. Its rounded, bounded value is an optional coarse hint, never available/free memory.
- [W3C WebGPU](https://www.w3.org/TR/webgpu/) supplies the adapter features and limits exposed to this origin; the prototype does not request a device or infer hardware details the browser omits.
- [W3C Service Workers](https://www.w3.org/TR/service-workers/) ties service-worker lifetime to events and permits user agents to terminate one at any time. This prototype therefore keeps the compute session page-owned; a future cache service worker must not own the task lease or compute loop.

## WorkContract boundary

The existing [WorkContract](../../src/long_haul/work/contracts.py) remains the authoritative objective, scope, budgets, and acceptance predicates. Flash should add a versioned optional execution extension to that contract, not replace the contract or create a second acceptance system. Candidate shape:

```ts
interface FlashExecutionExtensionV1 {
  schemaVersion: 1;
  runtimeClass: string;
  modelTier: string;
  hardResourceCeiling: { cpuThreads: number; applicationMemoryMiB?: number };
  permissionRequirements: string[];
}

interface WorkContractV1 {
  // Existing objective, scope, budget, and acceptance fields stay authoritative.
  flashExecution?: FlashExecutionExtensionV1;
}
```

This is a design sketch, not a schema change in this prototype. A future extension may declare a Flash runtime/model class, hard resource ceiling, and permission requirements. Permission metadata only asks for a capability; it never grants browser permission, and the local user action in [#158](https://github.com/pH34r-pH/long-haul/issues/158) remains authoritative. The queue delivery envelope should separately carry job/attempt/lease correlation; queue receipts and browser session IDs do not become WorkContract identity or authorization.

The client snapshot is advisory. A later gateway must choose only a pre-qualified tier, keep storage credentials server-side, reject expired/superseded claims, and accept results through the existing external-verification semantics. The durable Flash queue is the separate lane in [#157](https://github.com/pH34r-pH/long-haul/issues/157); known-vessel execution uses reviewed Fleet GitHub Actions with one persistent trusted runner per participating vessel and a shared foreground slot, as [#150](https://github.com/pH34r-pH/long-haul/issues/150) now specifies. HTCondor/DAGMan is deferred.

## Checks

Run the dependency-free contract tests with:

```sh
node --test prototypes/flash-vessel/tests/unit.test.mjs
```

When Playwright and Chromium are available, run the page/worker lifecycle test with:

```sh
node --test prototypes/flash-vessel/tests/browser.test.mjs
```
