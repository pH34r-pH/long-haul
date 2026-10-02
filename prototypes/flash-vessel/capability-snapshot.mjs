const SIMD_TEST_MODULE = Uint8Array.of(
  0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00,
  0x01, 0x05, 0x01, 0x60, 0x00, 0x01, 0x7b,
);

const GPU_LIMIT_NAMES = [
  "maxBufferSize",
  "maxStorageBufferBindingSize",
  "maxComputeWorkgroupSizeX",
  "maxComputeInvocationsPerWorkgroup",
  "maxComputeWorkgroupsPerDimension",
];

const available = (value) => ({ status: "available", value });
const unavailable = () => ({ status: "unavailable" });
const unknown = () => ({ status: "unknown" });

function readProperty(target, name) {
  if (target === null || target === undefined) return { found: false };
  try {
    if (!(name in Object(target))) return { found: false };
    return { found: true, value: target[name] };
  } catch {
    return { found: false, error: true };
  }
}

function numericHint(target, name, { integer = false } = {}) {
  const property = readProperty(target, name);
  if (!property.found) return unknown();
  const value = property.value;
  if (!Number.isFinite(value) || value <= 0 || (integer && !Number.isInteger(value))) {
    return unknown();
  }
  return available(value);
}

function simdHint(wasm) {
  if (typeof wasm?.validate !== "function") return unknown();
  try {
    return wasm.validate(SIMD_TEST_MODULE) ? available(true) : unavailable();
  } catch {
    return unknown();
  }
}

function sharedArrayBufferHint(sharedArrayBuffer, crossOriginIsolated) {
  if (typeof crossOriginIsolated !== "boolean") return unknown();
  if (!crossOriginIsolated || typeof sharedArrayBuffer !== "function") return unavailable();
  return available(true);
}

function gpuLimitHints(limits) {
  return Object.fromEntries(GPU_LIMIT_NAMES.map((name) => {
    const property = readProperty(limits, name);
    if (!property.found || !Number.isFinite(property.value) || property.value <= 0) {
      return [name, unknown()];
    }
    return [name, available(property.value)];
  }));
}

async function webGpuHint(navigatorLike, timeoutMs) {
  const gpuProperty = readProperty(navigatorLike, "gpu");
  if (!gpuProperty.found || typeof gpuProperty.value?.requestAdapter !== "function") return unknown();

  let adapterPromise;
  try {
    adapterPromise = Promise.resolve(gpuProperty.value.requestAdapter());
  } catch {
    return unknown();
  }

  let timeout;
  try {
    const adapter = await Promise.race([
      adapterPromise,
      new Promise((_, reject) => {
        timeout = setTimeout(() => reject(new Error("adapter request timed out")), timeoutMs);
      }),
    ]);
    if (adapter === null) return unavailable();
    if (adapter === undefined) return unknown();

    let features = unknown();
    try {
      if (adapter?.features && typeof adapter.features[Symbol.iterator] === "function") {
        features = available([...adapter.features].filter((feature) => typeof feature === "string").sort());
      }
    } catch {
      features = unknown();
    }

    return available({ features, limits: gpuLimitHints(adapter?.limits) });
  } catch {
    return unknown();
  } finally {
    clearTimeout(timeout);
  }
}

function normalizeVisibility(value) {
  return value === "visible" || value === "hidden" ? value : "unknown";
}

/**
 * Read only cheap, browser-exposed hints. This function measures no
 * throughput/latency, allocation pressure, or stress behavior.
 *
 * @param {object} options
 * @returns {Promise<import("./capability-snapshot.d.ts").CapabilitySnapshotV1>}
 */
export async function collectCapabilitySnapshot({
  navigatorLike = globalThis.navigator,
  wasm = globalThis.WebAssembly,
  sharedArrayBuffer = globalThis.SharedArrayBuffer,
  crossOriginIsolated = globalThis.crossOriginIsolated,
  visibility = "unknown",
  randomUUID = () => globalThis.crypto?.randomUUID?.(),
  adapterTimeoutMs = 1200,
} = {}) {
  const flashInstanceId = randomUUID();
  if (typeof flashInstanceId !== "string" || flashInstanceId.length === 0) {
    throw new Error("a secure-context random UUID source is required after explicit join");
  }

  return {
    schemaVersion: 1,
    flashInstanceId,
    visibility: normalizeVisibility(visibility),
    capabilities: {
      logicalProcessors: numericHint(navigatorLike, "hardwareConcurrency", { integer: true }),
      deviceMemoryGiB: numericHint(navigatorLike, "deviceMemory"),
      wasmSimd: simdHint(wasm),
      sharedArrayBuffer: sharedArrayBufferHint(sharedArrayBuffer, crossOriginIsolated),
      webGpu: await webGpuHint(navigatorLike, adapterTimeoutMs),
    },
  };
}
