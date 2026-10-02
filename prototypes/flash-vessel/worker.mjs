import { collectCapabilitySnapshot } from "./capability-snapshot.mjs";

let joined = false;
let stopped = false;
let visibility = "unknown";
let lifecycle = "idle";

function normalizeVisibility(value) {
  return value === "visible" || value === "hidden" ? value : "unknown";
}

function setVisibility(value) {
  visibility = normalizeVisibility(value);
  if (!joined || stopped) return;
  lifecycle = visibility === "hidden" ? "paused" : "active";
  self.postMessage({ type: "LIFECYCLE", lifecycle, visibility });
}

self.addEventListener("message", (event) => {
  const message = event.data;
  if (!message || typeof message.type !== "string" || stopped) return;

  if (message.type === "JOIN" && !joined) {
    joined = true;
    visibility = normalizeVisibility(message.visibility);
    lifecycle = visibility === "hidden" ? "paused" : "active";

    void collectCapabilitySnapshot({
      navigatorLike: self.navigator,
      wasm: self.WebAssembly,
      sharedArrayBuffer: self.SharedArrayBuffer,
      crossOriginIsolated: self.crossOriginIsolated,
      visibility,
      randomUUID: () => self.crypto.randomUUID(),
    }).then((snapshot) => {
      if (stopped) return;
      snapshot.visibility = visibility;
      self.postMessage({ type: "SNAPSHOT", lifecycle, snapshot });
    }).catch((error) => {
      if (stopped) return;
      self.postMessage({ type: "ERROR", message: String(error?.message ?? error) });
    });
    return;
  }

  if (message.type === "VISIBILITY") {
    setVisibility(message.visibility);
    return;
  }

  if (message.type === "STOP") {
    stopped = true;
    lifecycle = "stopped";
    self.postMessage({ type: "STOPPED", lifecycle, reason: message.reason ?? "user" });
    self.close();
  }
});
