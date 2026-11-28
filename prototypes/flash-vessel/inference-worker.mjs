import { collectCapabilitySnapshot } from "./capability-snapshot.mjs";
import {
  LIMITS,
  MODEL,
  previewCapability,
  runMatchingWork,
  validateMatchRequest,
} from "./local-workload.mjs";

let started = false;
let stopped = false;
let stopReason = "user";
let pipeline = null;

function post(type, details = {}) {
  if (!stopped) self.postMessage({ type, ...details });
}

async function disposePipeline() {
  const current = pipeline;
  pipeline = null;
  if (current && typeof current.dispose === "function") {
    await current.dispose();
  }
}

function safeErrorMessage(error) {
  if (!self.navigator.onLine) return "Model/runtime download stopped because this tab is offline.";
  if (error?.name === "AbortError") return "The local model load was cancelled.";
  return "The pinned local model or CPU runtime could not initialize. No work was accepted; try again after the cooldown.";
}

async function start(request, visibility, online) {
  try {
    const validRequest = validateMatchRequest(request);
    if (!online) {
      post("UNSUPPORTED", { reason: "offline" });
      return;
    }

    const snapshot = await collectCapabilitySnapshot({
      navigatorLike: self.navigator,
      wasm: self.WebAssembly,
      sharedArrayBuffer: self.SharedArrayBuffer,
      crossOriginIsolated: self.crossOriginIsolated,
      visibility,
      randomUUID: () => self.crypto.randomUUID(),
    });
    if (stopped) return;

    const capability = previewCapability(snapshot, { online, visibility });
    post("CAPABILITY", { snapshot, capability });
    if (!capability.supported) {
      post("UNSUPPORTED", { reason: capability.reason });
      return;
    }

    post("STATUS", { state: "loading", message: "Loading the pinned public model and runtime after your Start action…" });
    const { env, pipeline: createPipeline } = await import(MODEL.runtimeUrl);
    if (stopped) return;
    env.allowLocalModels = false;
    env.allowRemoteModels = true;
    env.useBrowserCache = true;
    env.backends.onnx.wasm.numThreads = LIMITS.cpuThreads;

    pipeline = await createPipeline("feature-extraction", MODEL.id, {
      revision: MODEL.revision,
      dtype: MODEL.dtype,
      progress_callback: (event) => {
        if (stopped) return;
        const progress = Number.isFinite(event?.progress) ? Math.max(0, Math.min(100, event.progress)) : null;
        post("PROGRESS", { fileStatus: event?.status ?? "loading", progress });
      },
    });
    if (stopped) {
      await disposePipeline();
      return;
    }

    post("STATUS", { state: "embedding", message: "Comparing the query with three notes in this worker…" });
    const result = await runMatchingWork({
      request: validRequest,
      extractor: pipeline,
      isCancelled: () => stopped,
      onProgress: (progress) => post("EMBEDDING_PROGRESS", progress),
    });
    if (stopped || result === null) return;

    post("RESULT", result);
    await disposePipeline();
    post("DONE");
  } catch (error) {
    if (stopped) return;
    await disposePipeline().catch(() => {});
    post("ERROR", { message: safeErrorMessage(error) });
  } finally {
    if (stopped) {
      await disposePipeline().catch(() => {});
      self.postMessage({ type: "STOPPED", reason: stopReason });
    }
    self.close();
  }
}

self.addEventListener("message", (event) => {
  const message = event.data;
  if (!message || typeof message.type !== "string") return;

  if (message.type === "START" && !started && !stopped) {
    started = true;
    void start(message.request, message.visibility, message.online);
    return;
  }

  if (message.type === "STOP" && !stopped) {
    stopped = true;
    stopReason = message.reason ?? "user";
    if (!started) {
      self.postMessage({ type: "STOPPED", reason: stopReason });
      self.close();
    }
  }
});
