import assert from "node:assert/strict";
import test from "node:test";

import { collectCapabilitySnapshot } from "../capability-snapshot.mjs";
import { forwardVisibilityChanges } from "../page-visibility.mjs";

const unknown = { status: "unknown" };
const unavailable = { status: "unavailable" };

test("missing browser hints remain explicitly unknown", async () => {
  const snapshot = await collectCapabilitySnapshot({
    navigatorLike: {},
    wasm: {},
    sharedArrayBuffer: undefined,
    crossOriginIsolated: undefined,
    visibility: "prerender",
    randomUUID: () => "tab-a",
  });

  assert.equal(snapshot.schemaVersion, 1);
  assert.equal(snapshot.flashInstanceId, "tab-a");
  assert.equal(snapshot.visibility, "unknown");
  assert.deepEqual(snapshot.capabilities.logicalProcessors, unknown);
  assert.deepEqual(snapshot.capabilities.deviceMemoryGiB, unknown);
  assert.deepEqual(snapshot.capabilities.wasmSimd, unknown);
  assert.deepEqual(snapshot.capabilities.sharedArrayBuffer, unknown);
  assert.deepEqual(snapshot.capabilities.webGpu, unknown);
});

test("exposed values are preserved without inferring headroom", async () => {
  const adapter = {
    features: new Set(["shader-f16", "timestamp-query"]),
    limits: {
      maxBufferSize: 1024,
      maxStorageBufferBindingSize: 512,
      maxComputeWorkgroupSizeX: 128,
      maxComputeInvocationsPerWorkgroup: 256,
      maxComputeWorkgroupsPerDimension: 65535,
    },
  };
  const snapshot = await collectCapabilitySnapshot({
    navigatorLike: {
      hardwareConcurrency: 8,
      deviceMemory: 4,
      gpu: { requestAdapter: async () => adapter },
    },
    wasm: { validate: () => true },
    sharedArrayBuffer: class SharedArrayBuffer {},
    crossOriginIsolated: true,
    visibility: "visible",
    randomUUID: () => "tab-b",
  });

  assert.equal(snapshot.capabilities.logicalProcessors.value, 8);
  assert.equal(snapshot.capabilities.deviceMemoryGiB.value, 4);
  assert.deepEqual(snapshot.capabilities.wasmSimd, { status: "available", value: true });
  assert.deepEqual(snapshot.capabilities.sharedArrayBuffer, { status: "available", value: true });
  assert.deepEqual(snapshot.capabilities.webGpu.value.features, {
    status: "available",
    value: ["shader-f16", "timestamp-query"],
  });
  assert.equal(snapshot.capabilities.webGpu.value.limits.maxBufferSize.value, 1024);
});

test("explicit negative reports differ from unknown and invalid values stay unknown", async () => {
  const snapshot = await collectCapabilitySnapshot({
    navigatorLike: {
      hardwareConcurrency: 0,
      deviceMemory: Number.NaN,
      gpu: { requestAdapter: async () => null },
    },
    wasm: { validate: () => false },
    sharedArrayBuffer: class SharedArrayBuffer {},
    crossOriginIsolated: false,
    visibility: "hidden",
    randomUUID: () => "tab-c",
  });

  assert.deepEqual(snapshot.capabilities.logicalProcessors, unknown);
  assert.deepEqual(snapshot.capabilities.deviceMemoryGiB, unknown);
  assert.deepEqual(snapshot.capabilities.wasmSimd, unavailable);
  assert.deepEqual(snapshot.capabilities.sharedArrayBuffer, unavailable);
  assert.deepEqual(snapshot.capabilities.webGpu, unavailable);
  assert.equal(snapshot.visibility, "hidden");
});

test("failed browser APIs do not turn into negative capability claims", async () => {
  const snapshot = await collectCapabilitySnapshot({
    navigatorLike: {
      get hardwareConcurrency() { throw new Error("withheld"); },
      gpu: { requestAdapter: async () => { throw new Error("unavailable to this context"); } },
    },
    wasm: { validate: () => { throw new Error("probe failed"); } },
    sharedArrayBuffer: undefined,
    crossOriginIsolated: null,
    visibility: "visible",
    randomUUID: () => "tab-d",
  });

  assert.deepEqual(snapshot.capabilities.logicalProcessors, unknown);
  assert.deepEqual(snapshot.capabilities.wasmSimd, unknown);
  assert.deepEqual(snapshot.capabilities.sharedArrayBuffer, unknown);
  assert.deepEqual(snapshot.capabilities.webGpu, unknown);
});

test("an adapter request that does not settle remains unknown", async () => {
  const snapshot = await collectCapabilitySnapshot({
    navigatorLike: { gpu: { requestAdapter: () => new Promise(() => {}) } },
    wasm: {},
    sharedArrayBuffer: undefined,
    crossOriginIsolated: undefined,
    visibility: "visible",
    randomUUID: () => "tab-e",
    adapterTimeoutMs: 1,
  });

  assert.deepEqual(snapshot.capabilities.webGpu, unknown);
});

test("instance identity requires a fresh secure random source", async () => {
  await assert.rejects(
    collectCapabilitySnapshot({ navigatorLike: {}, randomUUID: () => undefined }),
    /random UUID source/,
  );
});

test("page visibility events reach the current worker with the reported state", () => {
  let visibility = "visible";
  let worker = null;
  const messages = [];
  const documentLike = new EventTarget();
  Object.defineProperty(documentLike, "visibilityState", { get: () => visibility });
  const disconnect = forwardVisibilityChanges(documentLike, () => worker);

  documentLike.dispatchEvent(new Event("visibilitychange"));
  assert.deepEqual(messages, []);

  worker = { postMessage: (message) => messages.push(message) };
  visibility = "hidden";
  documentLike.dispatchEvent(new Event("visibilitychange"));
  assert.deepEqual(messages, [{ type: "VISIBILITY", visibility: "hidden" }]);

  visibility = "visible";
  documentLike.dispatchEvent(new Event("visibilitychange"));
  assert.deepEqual(messages.at(-1), { type: "VISIBILITY", visibility: "visible" });

  disconnect();
  visibility = "hidden";
  documentLike.dispatchEvent(new Event("visibilitychange"));
  assert.equal(messages.length, 2);
});
