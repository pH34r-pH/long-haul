import { forwardVisibilityChanges } from "./page-visibility.mjs";
import { REFERENCE_WORKLOAD, validateMatchRequest } from "./local-workload.mjs";

const joinButton = document.querySelector("#join");
const stopButton = document.querySelector("#stop");
const status = document.querySelector("#status");
const snapshotOutput = document.querySelector("#snapshot");

let worker = null;
let stopFallback = null;
const localStartButton = document.querySelector("#run-local");
const localStopButton = document.querySelector("#stop-local");
const localStatus = document.querySelector("#local-status");
const localResult = document.querySelector("#local-result");
const localQuery = document.querySelector("#local-query");
const localNotes = [1, 2, 3].map((index) => document.querySelector(`#local-note-${index}`));
const localInputs = [localQuery, ...localNotes];

let localWorker = null;
let localStopFallback = null;
let localStopRequested = false;
let localStopMessage = "Local matching stopped.";
let localFailureCount = 0;
let localCooldownTimer = null;
let localRetryAt = 0;

function renderState(state, message) {
  status.dataset.state = state;
  status.textContent = message;
  joinButton.disabled = state === "joining" || state === "active" || state === "paused" || state === "stopping";
  stopButton.disabled = !(state === "joining" || state === "active" || state === "paused");
}

function disposeWorker(current) {
  if (current === worker && stopFallback !== null) {
    clearTimeout(stopFallback);
    stopFallback = null;
  }
  current.terminate();
  if (worker === current) worker = null;
}

function setLocalState(state, message) {
  localStatus.dataset.state = state;
  localStatus.textContent = message;
  const busy = ["loading", "embedding", "stopping", "cleaning"].includes(state);
  localStartButton.disabled = busy || Date.now() < localRetryAt;
  localStopButton.disabled = !["loading", "embedding"].includes(state);
  for (const input of localInputs) input.disabled = busy;
}

function finishLocalSession(current, state, message) {
  if (current !== localWorker) return;
  if (localStopFallback !== null) {
    clearTimeout(localStopFallback);
    localStopFallback = null;
  }
  current.terminate();
  localWorker = null;
  localStopRequested = false;
  localStopMessage = "Local matching stopped.";
  setLocalState(state, message);
}

function failureCooldown(current, message) {
  localFailureCount += 1;
  const waitSeconds = Math.min(30, 2 ** (localFailureCount - 1));
  localRetryAt = Date.now() + waitSeconds * 1000;
  const cooldownMessage = `${message} Start is available again in ${waitSeconds}s; there is no automatic retry.`;
  if (current) finishLocalSession(current, "error", cooldownMessage);
  else setLocalState("error", cooldownMessage);
  if (localCooldownTimer !== null) clearTimeout(localCooldownTimer);
  localCooldownTimer = setTimeout(() => {
    localCooldownTimer = null;
    localRetryAt = 0;
    if (!localWorker && localStatus.dataset.state === "error") {
      setLocalState("idle", "Cooldown ended. Start again when you choose.");
    }
  }, waitSeconds * 1000);
}

function requestLocalStop(reason, { immediate = false, message = "Local matching stopped." } = {}) {
  const current = localWorker;
  if (!current) return;
  localStopRequested = true;
  localStopMessage = message;
  if (immediate) {
    finishLocalSession(current, "cancelled", message);
    return;
  }

  setLocalState("stopping", "Stopping the local worker and releasing its model…");
  current.postMessage({ type: "STOP", reason });
  localStopFallback = setTimeout(() => {
    finishLocalSession(current, "cancelled", message);
  }, 500);
}

function currentMatchRequest() {
  return validateMatchRequest({
    query: localQuery.value,
    candidates: localNotes.map((input, index) => ({
      id: REFERENCE_WORKLOAD.candidates[index].id,
      text: input.value,
    })),
  });
}

function displayLocalResult(result) {
  const summary = {
    ranking: result.matches,
    referenceCheck: result.reference ?? "No known-answer reference for edited inputs.",
    note: "Cosine similarity is a ranking hint, not a correctness or acceptance decision.",
  };
  localResult.textContent = JSON.stringify(summary, null, 2);
}

function unsupportedMessage(reason) {
  const reasons = {
    offline: "Unsupported while offline: the pinned public runtime may need to load.",
    "tab-not-visible": "Unsupported while hidden. Return to the tab and choose Start again.",
    "logical-processors-unknown": "Unsupported here: a logical processor hint is unknown.",
    "logical-processors-below-preview-floor": "Unsupported here: fewer than two logical processors were exposed.",
    "device-memory-unknown": "Unsupported here: the coarse device-memory hint is unknown.",
    "device-memory-below-preview-floor": "Unsupported here: the coarse memory hint is below this preview's 4 GiB floor.",
    "wasm-simd-unavailable-or-unknown": "Unsupported here: WebAssembly SIMD was not reported as available.",
  };
  return reasons[reason] ?? "Unsupported: required browser capability is unknown or unavailable.";
}

localStartButton.addEventListener("click", () => {
  if (localWorker || Date.now() < localRetryAt) return;
  let request;
  try {
    if (!isSecureContext) throw new Error("A secure browser context (HTTPS or localhost) is required.");
    if (!navigator.onLine) {
      setLocalState("unsupported", "Unsupported while offline: the pinned public runtime may need to load.");
      return;
    }
    if (document.visibilityState !== "visible") {
      setLocalState("unsupported", "Return to this visible tab and choose Start again.");
      return;
    }
    request = currentMatchRequest();
  } catch (error) {
    setLocalState("error", error.message);
    return;
  }

  localResult.textContent = "Waiting for the local worker…";
  localStopRequested = false;
  setLocalState("loading", "Starting the one-shot local check…");
  try {
    localWorker = new Worker(new URL("./inference-worker.mjs", import.meta.url), { type: "module" });
  } catch {
    localWorker = null;
    failureCooldown(null, "Could not start a browser worker.");
    return;
  }

  const current = localWorker;
  current.addEventListener("message", (event) => {
    const message = event.data;
    if (current !== localWorker || (localStopRequested && message?.type !== "STOPPED")) return;
    if (message?.type === "CAPABILITY") {
      snapshotOutput.textContent = JSON.stringify(message.snapshot, null, 2);
    } else if (message?.type === "UNSUPPORTED") {
      finishLocalSession(current, "unsupported", unsupportedMessage(message.reason));
    } else if (message?.type === "STATUS") {
      setLocalState(message.state, message.message);
    } else if (message?.type === "PROGRESS") {
      const progress = Number.isFinite(message.progress) ? ` (${Math.round(message.progress)}%)` : "";
      setLocalState("loading", `Loading pinned runtime/model${progress}…`);
    } else if (message?.type === "EMBEDDING_PROGRESS") {
      setLocalState("embedding", `Embedding text ${message.completed} of ${message.total} in this worker…`);
    } else if (message?.type === "RESULT") {
      displayLocalResult(message);
      setLocalState("cleaning", "Result ready. Releasing the model before this session ends…");
    } else if (message?.type === "ERROR") {
      failureCooldown(current, message.message ?? "Local inference failed.");
    } else if (message?.type === "DONE") {
      localFailureCount = 0;
      localRetryAt = 0;
      finishLocalSession(current, "complete", "Complete. The model worker was released; start again explicitly for another check.");
    } else if (message?.type === "STOPPED") {
      finishLocalSession(current, "cancelled", localStopMessage);
    }
  });
  current.addEventListener("error", (event) => {
    event.preventDefault();
    if (current === localWorker && !localStopRequested) {
      failureCooldown(current, "The local worker failed and was stopped.");
    }
  });
  current.postMessage({
    type: "START",
    request,
    visibility: document.visibilityState,
    online: navigator.onLine,
  });
});

localStopButton.addEventListener("click", () => requestLocalStop("user"));

function stopSession(reason = "user", immediate = false) {
  const current = worker;
  if (!current) return;

  renderState("stopping", "Stopping this tab's worker…");
  if (immediate) {
    disposeWorker(current);
    renderState("stopped", "Stopped. No worker is active.");
    return;
  }

  current.postMessage({ type: "STOP", reason });
  stopFallback = setTimeout(() => {
    disposeWorker(current);
    renderState("stopped", "Stopped. No worker is active.");
  }, 500);
}

joinButton.addEventListener("click", () => {
  if (worker) return;
  snapshotOutput.textContent = "Waiting for the browser capability snapshot…";
  renderState("joining", "Joining this tab… capability hints only; no work is being run.");

  try {
    worker = new Worker(new URL("./worker.mjs", import.meta.url), { type: "module" });
  } catch (error) {
    renderState("error", `Could not start a browser worker: ${error.message}`);
    return;
  }

  const current = worker;
  current.addEventListener("message", (event) => {
    if (current !== worker) return;
    const message = event.data;
    if (message?.type === "SNAPSHOT") {
      snapshotOutput.textContent = JSON.stringify(message.snapshot, null, 2);
      const paused = message.lifecycle === "paused";
      renderState(paused ? "paused" : "active", paused
        ? "Paused because this tab is hidden. No work is being run."
        : "Joined for this tab. Capability snapshot only; no work is being run.");
    } else if (message?.type === "LIFECYCLE") {
      const paused = message.lifecycle === "paused";
      renderState(paused ? "paused" : "active", paused
        ? "Paused because this tab is hidden. No work is being run."
        : "Joined for this tab. Capability snapshot only; no work is being run.");
    } else if (message?.type === "STOPPED") {
      disposeWorker(current);
      renderState("stopped", "Stopped. No worker is active.");
    } else if (message?.type === "ERROR") {
      disposeWorker(current);
      renderState("error", `Capability snapshot failed: ${message.message}`);
    }
  });
  current.addEventListener("error", (event) => {
    event.preventDefault();
    disposeWorker(current);
    renderState("error", "The browser worker failed and was stopped.");
  });

  current.postMessage({ type: "JOIN", visibility: document.visibilityState });
});

stopButton.addEventListener("click", () => stopSession("user"));

forwardVisibilityChanges(document, () => worker);

window.addEventListener("pagehide", () => stopSession("pagehide", true));
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") {
    requestLocalStop("tab-hidden", {
      immediate: true,
      message: "Cancelled because the tab was hidden. Return and choose Start again.",
    });
  }
});
window.addEventListener("offline", () => requestLocalStop("offline", {
  immediate: true,
  message: "Cancelled because the tab went offline. Return online, then choose Start again.",
}));
window.addEventListener("pagehide", () => {
  requestLocalStop("pagehide", { immediate: true, message: "Stopped because this tab is closing." });
  if (localCooldownTimer !== null) clearTimeout(localCooldownTimer);
});
