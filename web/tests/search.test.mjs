import assert from "node:assert/strict";
import test from "node:test";
import { DEFAULT_RERANK, SEARCH_NOTICE, makeSearchBody } from "../lib/search.ts";

test("rerank starts off and notice names both outbound inputs", () => {
  assert.equal(DEFAULT_RERANK, false);
  assert.match(SEARCH_NOTICE, /câu tìm kiếm và các candidate chunks được phép/);
  assert.match(SEARCH_NOTICE, /OpenRouter\/provider/);
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
