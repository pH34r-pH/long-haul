import { forwardVisibilityChanges } from "./page-visibility.mjs";

const joinButton = document.querySelector("#join");
const stopButton = document.querySelector("#stop");
const status = document.querySelector("#status");
const snapshotOutput = document.querySelector("#snapshot");

let worker = null;
let stopFallback = null;

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
