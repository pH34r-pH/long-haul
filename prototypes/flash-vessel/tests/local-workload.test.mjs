import assert from "node:assert/strict";
import test from "node:test";

import {
  LIMITS,
  MODEL,
  REFERENCE_WORKLOAD,
  previewCapability,
  rankMatches,
  runMatchingWork,
  validateMatchRequest,
} from "../local-workload.mjs";

function capabilitySnapshot(overrides = {}) {
  return {
    capabilities: {
      logicalProcessors: { status: "available", value: 8 },
      deviceMemoryGiB: { status: "available", value: 8 },
      wasmSimd: { status: "available", value: true },
      ...overrides,
    },
  };
}

test("the runtime and model are pinned and the preview envelope is finite", () => {
  assert.match(MODEL.revision, /^[a-f0-9]{40}$/);
  assert.equal(MODEL.dtype, "q4");
  assert.equal(LIMITS.cpuThreads, 1);
  assert.equal(LIMITS.candidateCount, 3);
  assert.equal(LIMITS.maxTokens, 128);
  assert.equal(LIMITS.maxTextCharacters, 280);
});

test("request validation caps candidate count and text length and rejects ambiguous ids", () => {
  assert.deepEqual(validateMatchRequest(REFERENCE_WORKLOAD).query, REFERENCE_WORKLOAD.query);
  assert.throws(() => validateMatchRequest({ ...REFERENCE_WORKLOAD, candidates: [] }), /exactly 3/);
  assert.throws(() => validateMatchRequest({
    ...REFERENCE_WORKLOAD,
    candidates: [REFERENCE_WORKLOAD.candidates[0], REFERENCE_WORKLOAD.candidates[0], REFERENCE_WORKLOAD.candidates[2]],
  }), /unique/);
  assert.throws(() => validateMatchRequest({
    ...REFERENCE_WORKLOAD,
    query: "x".repeat(LIMITS.maxTextCharacters + 1),
  }), /characters/);
});

test("preview capability fails closed without inferring headroom", () => {
  assert.equal(previewCapability(capabilitySnapshot()).supported, true);
  assert.equal(previewCapability(capabilitySnapshot(), { online: false }).reason, "offline");
  assert.equal(previewCapability(capabilitySnapshot(), { visibility: "hidden" }).reason, "tab-not-visible");
  assert.equal(previewCapability(capabilitySnapshot({
    deviceMemoryGiB: { status: "unknown" },
  })).reason, "device-memory-unknown");
  assert.equal(previewCapability(capabilitySnapshot({
    wasmSimd: { status: "unavailable" },
  })).reason, "wasm-simd-unavailable-or-unknown");
  assert.equal(previewCapability(capabilitySnapshot({
    logicalProcessors: { status: "available", value: 1 },
  })).reason, "logical-processors-below-preview-floor");
});

test("rankings use cosine similarity and the independent reference is only bound to its exact input", () => {
  const exact = rankMatches(REFERENCE_WORKLOAD, [
    [1, 0],
    [0.9, 0.1],
    [0, 1],
    [-1, 0],
  ]);
  assert.equal(exact.matches[0].id, "delivery");
  assert.deepEqual(exact.reference, {
    expectedTopCandidateId: "delivery",
    observedTopCandidateId: "delivery",
    passed: true,
  });

  const edited = validateMatchRequest({
    ...REFERENCE_WORKLOAD,
    query: `${REFERENCE_WORKLOAD.query} Today.`,
  });
  assert.equal(rankMatches(edited, [[1, 0], [1, 0], [0, 1], [-1, 0]]).reference, null);
});

test("malformed or inconsistent embeddings fail instead of emitting a result", () => {
  assert.throws(() => rankMatches(REFERENCE_WORKLOAD, [[1, 0], [1, 0]]), /one query/);
  assert.throws(() => rankMatches(REFERENCE_WORKLOAD, [
    [1, 0], [1, 0], [0, 1], [0, Number.NaN],
  ]), /invalid embedding/);
  assert.throws(() => rankMatches(REFERENCE_WORKLOAD, [
    [1, 0], [1, 0], [0, 1, 0], [0, 0],
  ]), /dimensions/);
});

test("work embeds one query and three notes sequentially and releases every output", async () => {
  const texts = [];
  let disposed = 0;
  const vectors = new Map([
    [REFERENCE_WORKLOAD.query, [1, 0]],
    [REFERENCE_WORKLOAD.candidates[0].text, [0.9, 0.1]],
    [REFERENCE_WORKLOAD.candidates[1].text, [0, 1]],
    [REFERENCE_WORKLOAD.candidates[2].text, [-1, 0]],
  ]);
  const result = await runMatchingWork({
    request: REFERENCE_WORKLOAD,
    extractor: async (text, options) => {
      texts.push(text);
      assert.equal(options.max_length, LIMITS.maxTokens);
      assert.equal(options.truncation, true);
      return { data: Float32Array.from(vectors.get(text)), dispose: () => { disposed += 1; } };
    },
  });

  assert.equal(texts.length, 4);
  assert.equal(disposed, 4);
  assert.equal(result.reference.passed, true);
});

test("cancellation between embeddings prevents later texts from running", async () => {
  let cancelled = false;
  let calls = 0;
  const result = await runMatchingWork({
    request: REFERENCE_WORKLOAD,
    extractor: async () => {
      calls += 1;
      return { data: Float32Array.from([1, 0]) };
    },
    onProgress: () => { cancelled = true; },
    isCancelled: () => cancelled,
  });

  assert.equal(result, null);
  assert.equal(calls, 1);
});
