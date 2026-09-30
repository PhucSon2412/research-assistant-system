import assert from "node:assert/strict";
import test from "node:test";
import { DEFAULT_RERANK, SEARCH_NOTICE, makeSearchBody, sourceKindLabel } from "../lib/search.ts";

test("rerank starts off and notice names both outbound inputs", () => {
  assert.equal(DEFAULT_RERANK, false);
  assert.match(SEARCH_NOTICE, /câu tìm kiếm và các candidate chunks được phép/);
  assert.match(SEARCH_NOTICE, /OpenRouter\/provider/);
});

test("notice scopes common copies to their rights and keeps RAG private (M6b-2b3a)", () => {
  assert.match(SEARCH_NOTICE, /bản chung mà quyền Admin đã ghi cho phép tìm kiếm toàn văn/);
  assert.match(SEARCH_NOTICE, /collection chỉ thu hẹp phạm vi/);
  assert.match(SEARCH_NOTICE, /chỉ có metadata hoặc đã thu hồi không vào tìm kiếm/);
  assert.match(SEARCH_NOTICE, /chỉ được gửi khi quyền cho phép xử lý qua provider/);
  assert.match(SEARCH_NOTICE, /Hỏi đáp \(RAG\) chưa dùng bản chung/);
  assert.equal(sourceKindLabel("common"), "Bản chung");
  assert.equal(sourceKindLabel("private"), "Paper riêng của bạn");
});

test("search request includes the chosen rerank state", () => {
  assert.deepEqual(makeSearchBody("synthetic", "keyword", DEFAULT_RERANK, " "), {
    query: "synthetic", mode: "keyword", rerank: false, item_id: null, collection_id: null,
  });
  assert.deepEqual(makeSearchBody("synthetic", "hybrid", true, " paper-id "), {
    query: "synthetic", mode: "hybrid", rerank: true, item_id: "paper-id", collection_id: null,
  });
  assert.deepEqual(makeSearchBody("synthetic", "keyword", false, "", "collection-id"), {
    query: "synthetic", mode: "keyword", rerank: false, item_id: null, collection_id: "collection-id",
  });
});
