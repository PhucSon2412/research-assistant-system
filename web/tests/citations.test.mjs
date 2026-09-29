import assert from "node:assert/strict";
import test from "node:test";
import { RELATION_NOTE, citationStatusText, referenceLocator } from "../lib/citations.ts";

test("citation statuses never present unlinked or withdrawn records as available", () => {
  assert.match(citationStatusText("linked"), /chính xác/);
  for (const status of ["no_arxiv_id", "bare_identifier", "invalid_arxiv_id", "multiple_arxiv_ids",
    "conflicting_versions"]) {
    assert.match(citationStatusText(status), /^Chưa liên kết/);
  }
  assert.match(citationStatusText("discovery_record_deleted"), /không có đích khả dụng/);
  assert.equal(citationStatusText("future_status"), "future_status");
});

test("relation note states the edge is not agreement", () => {
  assert.match(RELATION_NOTE, /trích dẫn/);
  assert.match(RELATION_NOTE, /không biểu thị đồng ý/);
});

test("reference locators keep page and offset units", () => {
  assert.equal(referenceLocator({ locator_kind: "pdf", page: 4, start_offset: 3, end_offset: 9 }),
    "trang 4 · offset 3–9 trong text trang");
  assert.equal(referenceLocator({ locator_kind: "markdown", page: null, start_offset: 0, end_offset: 5 }),
    "offset 0–5 trong Markdown gốc");
});
