import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, api, describeError } from "../lib/api.ts";
import { RAG_ALL_SCOPE_OPTION, RAG_NOTICE, makeRagBody, ragReasonText, ragScopeText, ragWithheldText }
  from "../lib/rag.ts";

test("RAG UI submits exactly one chosen scope", () => {
  assert.deepEqual(makeRagBody("item", "paper-1", "Q", "keyword"),
    { item_id: "paper-1", question: "Q", mode: "keyword" });
  assert.deepEqual(makeRagBody("collection-1", "paper-1", "Q", "hybrid"),
    { collection_id: "collection-1", question: "Q", mode: "hybrid" });
  assert.deepEqual(makeRagBody("all", "paper-1", "Q", "keyword"),
    { scope: "all", question: "Q", mode: "keyword" });
});

test("RAG scope texts name private papers and permitted common copies, never 'private only' (M6b-2b3b)", () => {
  assert.match(RAG_NOTICE, /OpenRouter\/provider/);
  assert.match(RAG_NOTICE, /bản chung mà quyền Admin đã ghi cho phép tìm kiếm toàn văn và xử lý qua provider/);
  assert.match(RAG_NOTICE, /collection chỉ thu hẹp phạm vi, không cấp quyền/);
  assert.match(RAG_NOTICE, /danh mục tài liệu tham khảo cũng không/);
  for (const text of [RAG_NOTICE, RAG_ALL_SCOPE_OPTION, ragScopeText("all"), ragScopeText("item"),
    ragScopeText("collection", "Pair")]) {
    assert.doesNotMatch(text, /chưa dùng|chỉ dùng paper riêng/);
  }
  assert.match(RAG_ALL_SCOPE_OPTION, /bản chung được phép hỏi đáp/);
  assert.equal(ragScopeText("item"), "paper này");
  assert.equal(ragScopeText("collection", "Pair"), "collection Pair (chỉ các paper được phép trong đó)");
  assert.match(ragScopeText("all"), /bản chung được phép hỏi đáp/);
});

test("RAG reasons and withheld evidence have plain messages; unknown reasons show nothing", () => {
  assert.match(ragReasonText("evidence_not_permitted_for_provider"), /không cho gửi tới provider/);
  assert.match(ragReasonText("comparison_requires_multiple_papers"), /hai paper/);
  assert.match(ragReasonText("keyword_query_without_terms"), /từ phổ biến/);
  assert.equal(ragReasonText(undefined), null);
  assert.equal(ragReasonText("toString"), null);
  assert.equal(ragWithheldText(0), null);
  assert.equal(ragWithheldText(undefined), null);
  assert.match(ragWithheldText(2), /^2 đoạn tìm thấy thuộc bản chung/);
});

test("RAG scope change and pricing/provider errors have clear messages", () => {
  assert.equal(describeError(new ApiError(409, "rag_scope_changed")),
    "Quyền hoặc phạm vi của nguồn đã thay đổi trong lúc xử lý; câu trả lời không được hiển thị. Hãy hỏi lại.");
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
