import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, api, describeError } from "../lib/api.ts";
import { makeRagBody } from "../lib/rag.ts";

test("RAG UI submits exactly one chosen scope", () => {
  assert.deepEqual(makeRagBody("item", "paper-1", "Q", "keyword"),
    { item_id: "paper-1", question: "Q", mode: "keyword" });
  assert.deepEqual(makeRagBody("collection-1", "paper-1", "Q", "hybrid"),
    { collection_id: "collection-1", question: "Q", mode: "hybrid" });
  assert.deepEqual(makeRagBody("all", "paper-1", "Q", "keyword"),
    { scope: "all", question: "Q", mode: "keyword" });
});

test("RAG form request shows a clear message for pricing/provider 503", async () => {
  const previousDocument = globalThis.document;
  const previousFetch = globalThis.fetch;
  globalThis.document = { cookie: "ras_csrf=synthetic" };
  globalThis.fetch = async () => new Response(
    JSON.stringify({ error: "rag_generation_unavailable_unverified_pricing" }),
    { status: 503, headers: { "content-type": "application/json" } },
  );
  try {
    await assert.rejects(
      api("/rag/ask", { method: "POST", body: { item_id: "synthetic", question: "Q", mode: "keyword" } }),
      (err) => err instanceof ApiError && err.status === 503
        && describeError(err) === "Hỏi đáp hiện chưa khả dụng vì chi phí và provider chưa được xác minh.",
    );
  } finally {
    globalThis.document = previousDocument;
    globalThis.fetch = previousFetch;
  }
});

test("other API errors keep their existing message handling", () => {
  assert.equal(describeError(new ApiError(404, "not_found")), "Không tìm thấy.");
  assert.equal(describeError(new ApiError(503, "other_failure")), "Lỗi (other_failure).");
});
