# Flash Vessel browser-local preview

**Status: experimental local inference preview. It is not part of Long Haul admission, is not an approved Flash tier, and does not connect to the queue or Fleet.**

The page keeps the original opt-in capability snapshot and adds one bounded, user-started semantic matching workload. A dedicated worker uses a pinned Transformers.js runtime and MiniLM model to rank three short notes against one short query. The result is an informational similarity ranking, not a fact check or WorkContract acceptance. The only network requests after **Start local matching** are public runtime/model asset downloads; query text and results are handled in the tab and are not uploaded by this prototype.

## Run locally

Serve this directory over localhost or HTTPS, then open `index.html`. Before an explicit Start or Join action, the page creates no worker and loads no model runtime. **Join for this tab** reads the capability snapshot only. **Start local matching** creates a separate page-owned worker and begins the one-shot inference. **Stop local matching**, hiding the tab, going offline, or closing the page cancels it. A cooperative stop disposes the pipeline; the page terminates the worker after a short fallback if it does not respond. Returning to the tab does not resume work. A new Start creates a fresh worker and reruns the check.

The inference preview uses CPU/WebAssembly only: one runtime thread, one query, three notes, at most 280 characters per input and 128 model tokens per text. It requires visible/online state, WebAssembly SIMD, at least two exposed logical processors, and a coarse `deviceMemory` hint of at least 4 GiB. Unknown or unmet hints produce an explicit unsupported state. Those checks are a narrow prototype gate, not a capacity measurement or qualification. The model's pinned q4 weights are about 55 MB; browser caching may retain model assets, but inputs, results, and instance IDs are not stored. No GPU, sensor, Compute Pressure API, queue, telemetry endpoint, credentials, or Fleet service is used by the workload.

The fixed example has a separate deterministic expected-top-note check. It reports pass/fail only when all example inputs are unchanged. Edited inputs receive a ranking but no expected answer; similarity does not imply truth. Model initialization errors show an error and use bounded manual retry backoff (1, 2, 4, 8, 16, then 30 seconds). There is no automatic retry, admission, or result submission. This model-based tool remains outside the public F0 WorkContract, which still has no model.

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
- WebGPU adapter features and five numeric limits used as possible future admission inputs by the separate capability-only session.

No browser fingerprint, adapter name, stable vessel identity, or device history is collected. `navigator.storage.estimate()`, Compute Pressure, WebNN, and sensors are outside this implementation.

## Browser standards used

- [WHATWG HTML Web Workers](https://html.spec.whatwg.org/multipage/workers.html) defines the page-owned Dedicated Worker and message channel used here. The compute loop belongs in that worker, not the page's rendering thread.
- The [W3C Device Memory API](https://www.w3.org/TR/device-memory/) is a secure-context Working Draft. Its rounded, bounded value is an optional coarse hint, never available/free memory.
- [W3C WebGPU](https://www.w3.org/TR/webgpu/) supplies the adapter features and limits exposed to the capability snapshot; the workload does not use WebGPU or infer hardware details the browser omits.
- [W3C WebNN](https://www.w3.org/TR/webnn/) defines a hardware-agnostic inference abstraction. API presence alone does not show that an NPU is present or selected, and this preview does not use WebNN.
- The WebGPU [`GPUDevice.lost`](https://www.w3.org/TR/webgpu/#dom-gpudevice-lost) promise is the future device-loss signal; the CPU-only preview has no GPU device to lose.
- [Transformers.js pipeline documentation](https://huggingface.co/docs/transformers.js/en/pipelines) describes browser-side feature extraction, quantized weights, and revision pinning. The selected [MiniLM model card](https://huggingface.co/Xenova/all-MiniLM-L6-v2) exposes ONNX weights and an Apache-2.0 license.
- [W3C Service Workers](https://www.w3.org/TR/service-workers/) ties service-worker lifetime to events and permits user agents to terminate one at any time. This prototype therefore keeps the compute session page-owned; a future cache service worker must not own the task lease or compute loop.

## Follow-up: opt-in session calibration and registration

This is a design proposal only. It does not change the current F0 contract, authorize a per-worker benchmark, register a vessel, mint a token, or add a heartbeat or queue. Quick Join should continue to use a conservative predeclared envelope. The public Flash policy currently says not to benchmark each worker for admission; a separately consented registration/calibration path needs an explicit reviewed policy decision before implementation or queue use.

For that follow-up, keep a calibration session separate from Quick Join and make it optional with a short user-selected duration (suggested 5-minute default, 20-minute hard maximum). Compare only model/workload-specific paths that the browser and chosen runtime actually support. Use a bounded real workload with fixed input, a small number of repeat observations, cancellation, and conservative stop limits; target a sustainable rate for that workload, not failure or a whole-device capacity claim. Do not use a calibration result as a generic device score.

No backend priority is selected by this proposal. WebGPU is a GPU API with explicit device-loss handling; WebNN is a hardware-agnostic neural-network abstraction and does not establish that an NPU is present or selected. WebGL is not assumed to exist or serve as a compute fallback. A future implementation should first verify that the particular runtime can execute the pinned workload on an actually available backend, then compare only compatible available paths. The current preview deliberately uses the same single-thread CPU/WebAssembly path on every supported device.

An eventual registered session should have only a random in-memory session token and a short-lived heartbeat lease while the page is visible and work is active. Closing/crashing the tab or losing heartbeats expires it. Keep no browser fingerprint, stable vessel ID, or cross-session calibration history; users redo the optional calibration each session. A lost device, hidden page, offline state, explicit Stop, or runtime failure cancels the run. Retries require a manual restart after bounded backoff. No sensor access is needed.

Before implementation, the follow-up plan should specify a privacy/security review, exact pinned model/runtime/workload, backend-specific capability checks, cancellation and lease semantics, bounded retry behavior, and independent correctness checks. It must also reconcile with Long Haul [#156](https://github.com/pH34r-pH/long-haul/issues/156) and the existing bounded native benchmark work ([#116](https://github.com/pH34r-pH/long-haul/pull/116), [#129](https://github.com/pH34r-pH/long-haul/pull/129)); those measurements are not browser qualification evidence. Any browser/Fleet admission would still need its own reviewed qualification and protected execution.

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

The Python WorkContract now accepts an optional versioned `flash_execution` extension in `src/long_haul/work/contracts.py`. It is deliberately limited to F0: one CPU thread, no model, no GPU, and no sensors. The extension has no qualified application-memory ceiling yet; absent/unknown capability data or failed initialization declines the work. The 32 MiB value in `tests/fixtures/work/flash-f0-toy.json` is explicitly synthetic test input only, not a hardware qualification or a visitor admission setting. The private qualification record in DSL #580 remains private and must supply evidence before any production ceiling is set. Permission metadata only asks for a capability; it never grants browser permission, and the local user action in [#158](https://github.com/pH34r-pH/long-haul/issues/158) remains authoritative.

`src/long_haul/work/flash_delivery.py` is a deterministic in-memory contract fixture, not a queue adapter. It binds a copy of the existing WorkContract, enforces its `max_attempts` and `wall_seconds`, separates `contract_id` from `job_id`, `attempt_id`, and `lease_id`, and fences stale leases after expiry/redelivery. Completion requires a trusted external verifier that evaluates the contract; the verifier cannot be supplied by a worker. Rejected results leave the lease available for a valid retry. Accepted output is stored as canonical JSON and returned as detached copies, so caller mutation cannot change the accepted record. A repeated identical completion with the same lease is idempotent after a lost response. It does not contact Azure or store artifacts.

The later operations view may link to the existing authenticated Fleet dashboard for read-only check-in. Check-in does not join a queue or donate compute, and private fleet telemetry does not become public by default. This local preview adds no gateway, credentials, queue, hardware qualification, or private data export.

The client snapshot is advisory. A later gateway must choose only a pre-qualified tier, keep storage credentials server-side, reject expired/superseded claims, and accept results through the existing external-verification semantics. The durable Flash queue is the separate lane in [#157](https://github.com/pH34r-pH/long-haul/issues/157); known-vessel execution uses reviewed Fleet GitHub Actions with one persistent trusted runner per participating vessel and a shared foreground slot, as [#150](https://github.com/pH34r-pH/long-haul/issues/150) now specifies. HTCondor/DAGMan is deferred.

## Checks

Run the dependency-free browser workload and capability tests with:

```sh
node --test prototypes/flash-vessel/tests/unit.test.mjs
node --test prototypes/flash-vessel/tests/local-workload.test.mjs
```

When Playwright and Chromium are available, run the page/worker lifecycle test with:

```sh
node --test prototypes/flash-vessel/tests/browser.test.mjs
```

The browser suite uses an injected test runtime for repeatable lifecycle and cancellation checks. To fetch the pinned public assets and verify one real local model result in a browser, additionally set `FLASH_LIVE_MODEL_TEST=1`; this is manual preview evidence, not qualification. If that test environment requires a TLS proxy, set `FLASH_PROXY_CA_PATH` to its trusted CA file; the test installs it only into a disposable browser profile and keeps certificate verification enabled. The current page deployment workflow stages only the explicit static UI allowlist.
