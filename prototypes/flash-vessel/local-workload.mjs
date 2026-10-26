export const MODEL = Object.freeze({
  id: "Xenova/all-MiniLM-L6-v2",
  revision: "751bff37182d3f1213fa05d7196b954e230abad9",
  dtype: "q4",
  runtimeUrl: "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.8.1",
});

export const LIMITS = Object.freeze({
  maxTextCharacters: 280,
  candidateCount: 3,
  cpuThreads: 1,
  maxTokens: 128,
  minLogicalProcessors: 2,
  minDeviceMemoryGiB: 4,
});

export const REFERENCE_WORKLOAD = Object.freeze({
  query: "A parcel arrived late and needs a new delivery time.",
  candidates: Object.freeze([
    Object.freeze({
      id: "delivery",
      text: "Arrange a new delivery appointment for the delayed parcel.",
    }),
    Object.freeze({
      id: "account",
      text: "Reset a forgotten password for an online account.",
    }),
    Object.freeze({
      id: "recipe",
      text: "Choose vegetables for a roasted dinner.",
    }),
  ]),
  expectedTopCandidateId: "delivery",
});

function requireText(value, name) {
  if (typeof value !== "string") throw new TypeError(`${name} must be text`);
  const text = value.trim();
  if (text.length === 0 || text.length > LIMITS.maxTextCharacters || text.includes("\0")) {
    throw new RangeError(`${name} must contain 1–${LIMITS.maxTextCharacters} characters`);
  }
  return text;
}

export function validateMatchRequest(request) {
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    throw new TypeError("work request must be an object");
  }
  if (!Array.isArray(request.candidates) || request.candidates.length !== LIMITS.candidateCount) {
    throw new RangeError(`provide exactly ${LIMITS.candidateCount} candidate notes`);
  }

  const ids = new Set();
  const candidates = request.candidates.map((candidate, index) => {
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) {
      throw new TypeError(`candidate ${index + 1} must be an object`);
    }
    if (typeof candidate.id !== "string" || !/^[a-z][a-z0-9-]{0,31}$/.test(candidate.id)) {
      throw new TypeError(`candidate ${index + 1} has an invalid id`);
    }
    if (ids.has(candidate.id)) throw new TypeError("candidate ids must be unique");
    ids.add(candidate.id);
    return Object.freeze({
      id: candidate.id,
      text: requireText(candidate.text, `candidate ${index + 1}`),
    });
  });

  return Object.freeze({
    query: requireText(request.query, "query"),
    candidates: Object.freeze(candidates),
  });
}

function sameReferenceRequest(request) {
  if (request.query !== REFERENCE_WORKLOAD.query) return false;
  return request.candidates.length === REFERENCE_WORKLOAD.candidates.length
    && request.candidates.every((candidate, index) => {
      const expected = REFERENCE_WORKLOAD.candidates[index];
      return candidate.id === expected.id && candidate.text === expected.text;
    });
}

export function previewCapability(snapshot, { online = true, visibility = "visible" } = {}) {
  if (!online) return { supported: false, reason: "offline" };
  if (visibility !== "visible") return { supported: false, reason: "tab-not-visible" };

  const capabilities = snapshot?.capabilities;
  const processors = capabilities?.logicalProcessors;
  if (processors?.status !== "available") {
    return { supported: false, reason: "logical-processors-unknown" };
  }
  if (processors.value < LIMITS.minLogicalProcessors) {
    return { supported: false, reason: "logical-processors-below-preview-floor" };
  }

  const memory = capabilities?.deviceMemoryGiB;
  if (memory?.status !== "available") {
    return { supported: false, reason: "device-memory-unknown" };
  }
  if (memory.value < LIMITS.minDeviceMemoryGiB) {
    return { supported: false, reason: "device-memory-below-preview-floor" };
  }

  const simd = capabilities?.wasmSimd;
  if (simd?.status !== "available" || simd.value !== true) {
    return { supported: false, reason: "wasm-simd-unavailable-or-unknown" };
  }

  return {
    supported: true,
    cpuThreads: LIMITS.cpuThreads,
    maxTokens: LIMITS.maxTokens,
    note: "Experimental local preview only; this is not an approved Flash tier.",
  };
}

function vectorFromOutput(output) {
  const data = output?.data ?? output;
  if (!(Array.isArray(data) || ArrayBuffer.isView(data))) {
    throw new TypeError("model returned no embedding vector");
  }
  const values = Array.from(data, Number);
  if (values.length === 0 || values.some((value) => !Number.isFinite(value))) {
    throw new TypeError("model returned an invalid embedding vector");
  }
  return values;
}

export function rankMatches(request, outputs) {
  const validRequest = validateMatchRequest(request);
  if (!Array.isArray(outputs) || outputs.length !== validRequest.candidates.length + 1) {
    throw new RangeError("one query and one embedding per candidate are required");
  }
  const vectors = outputs.map(vectorFromOutput);
  const query = vectors[0];
  if (vectors.some((vector) => vector.length !== query.length)) {
    throw new RangeError("embedding dimensions do not match");
  }

  const queryNorm = Math.sqrt(query.reduce((sum, value) => sum + value * value, 0));
  if (queryNorm === 0) throw new RangeError("query embedding has zero length");

  const matches = validRequest.candidates.map((candidate, index) => {
    const vector = vectors[index + 1];
    const norm = Math.sqrt(vector.reduce((sum, value) => sum + value * value, 0));
    if (norm === 0) throw new RangeError(`embedding for ${candidate.id} has zero length`);
    const dot = query.reduce((sum, value, position) => sum + value * vector[position], 0);
    return {
      id: candidate.id,
      score: Number((dot / (queryNorm * norm)).toFixed(6)),
    };
  }).sort((left, right) => right.score - left.score || left.id.localeCompare(right.id));

  const reference = sameReferenceRequest(validRequest)
    ? {
      expectedTopCandidateId: REFERENCE_WORKLOAD.expectedTopCandidateId,
      observedTopCandidateId: matches[0]?.id ?? null,
      passed: matches[0]?.id === REFERENCE_WORKLOAD.expectedTopCandidateId,
    }
    : null;

  return { matches, reference };
}

export async function runMatchingWork({ request, extractor, isCancelled = () => false, onProgress = () => {} }) {
  const validRequest = validateMatchRequest(request);
  const texts = [validRequest.query, ...validRequest.candidates.map(({ text }) => text)];
  const embeddings = [];

  for (const [index, text] of texts.entries()) {
    if (isCancelled()) return null;
    const output = await extractor(text, {
      pooling: "mean",
      normalize: true,
      truncation: true,
      max_length: LIMITS.maxTokens,
    });
    try {
      if (isCancelled()) return null;
      embeddings.push(vectorFromOutput(output));
    } finally {
      await output?.dispose?.();
    }
    onProgress({ completed: index + 1, total: texts.length });
    await new Promise((resolve) => setTimeout(resolve, 0));
  }

  return rankMatches(validRequest, embeddings);
}
