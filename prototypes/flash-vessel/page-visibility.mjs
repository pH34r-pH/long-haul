export function forwardVisibilityChanges(documentLike, getWorker) {
  const handleVisibilityChange = () => {
    const worker = getWorker();
    if (worker) {
      worker.postMessage({ type: "VISIBILITY", visibility: documentLike.visibilityState });
    }
  };

  documentLike.addEventListener("visibilitychange", handleVisibilityChange);
  return () => documentLike.removeEventListener("visibilitychange", handleVisibilityChange);
}
