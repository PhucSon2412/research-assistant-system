import assert from "node:assert/strict";
import test from "node:test";
import { DUPLICATE_KEPT, DUPLICATE_NO_TARGET, duplicateNotice, existingCopyHref } from "../lib/duplicates.ts";

// Synthetic IDs only (M6e).
const ID = "0f1e2d3c-0000-4000-8000-00000000000f";

test("valid existing copy links only to the API-given item, with its real status", () => {
  const ready = duplicateNotice({ item_id: ID, title: "Bài A", content_status: "indexed" });
  assert.equal(ready.href, `/items/${ID}`);
  assert.match(ready.label, /Bài A \(đã lập chỉ mục\)/);
  assert.match(ready.text, /cùng SHA-256/);
  assert.ok(ready.text.includes(DUPLICATE_KEPT));
  const pending = duplicateNotice({ item_id: ID, title: null, content_status: "metadata_only" });
  assert.match(pending.label, /\(chưa có tiêu đề\) \(đang xử lý hoặc chờ lập chỉ mục\)/);
});

test("duplicate itself is never called indexed, merged or reused", () => {
  for (const notice of [duplicateNotice({ item_id: ID, title: "x", content_status: "indexed" }), duplicateNotice(null)]) {
    assert.match(notice.text, /không được trích xuất, lập chỉ mục/);
    assert.match(notice.text, /không liên kết, hợp nhất hoặc dùng lại/);
    assert.doesNotMatch(notice.text, /canonical|đã liên kết với|đã hợp nhất/);
  }
});

test("no valid target: no link, no stale id/title, original kept", () => {
  for (const copy of [null, undefined, { item_id: "../admin", title: "Old", content_status: "indexed" },
    { item_id: `${ID}/x`, title: "Old", content_status: "indexed" }, { item_id: "", title: "Old", content_status: "indexed" }]) {
    const notice = duplicateNotice(copy);
    assert.equal(notice.href, null);
    assert.equal(notice.label, null);
    assert.equal(notice.text, DUPLICATE_NO_TARGET);
    assert.doesNotMatch(notice.text, /Old/);
    assert.equal(existingCopyHref(copy), null);
  }
  assert.match(DUPLICATE_NO_TARGET, /file gốc mới được giữ/);
});
