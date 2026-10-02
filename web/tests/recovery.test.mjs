import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, api, describeError } from "../lib/api.ts";

test("recovery gate and journal errors have plain messages (M6c-1)", () => {
  assert.match(describeError(new ApiError(503, "service_recovering")), /tạm ngừng phục vụ/);
  assert.match(describeError(new ApiError(503, "revocation_journal_unavailable")), /đã bị thu hồi.*nhật ký/);
  assert.match(describeError(new ApiError(503, "revocation_pending_recovery")), /chưa áp dụng.*phục hồi/);
  // An unfinished revocation is never shown as "not revoked".
  for (const code of ["revocation_journal_unavailable", "revocation_pending_recovery"]) {
    assert.doesNotMatch(describeError(new ApiError(503, code)), /chưa bị thu hồi/);
  }
});

test("a 503 from the recovery gate reaches the UI as service_recovering", async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ error: "service_recovering" }),
    { status: 503, headers: { "content-type": "application/json", "retry-after": "60" } });
  try {
    await assert.rejects(api("/library/items"),
      (err) => err instanceof ApiError && err.status === 503 && err.code === "service_recovering");
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("review-closure journal errors never claim the grant is still open when it ended (M6c-2b1)", () => {
  assert.match(describeError(new ApiError(503, "review_journal_unavailable")), /đã đóng.*nhật ký/);
  assert.match(describeError(new ApiError(503, "review_closure_pending_recovery")), /chưa áp dụng.*phục hồi/);
  // Only the window where neither the journal nor the database took the close says it is still open.
  assert.match(describeError(new ApiError(503, "review_closure_unavailable")), /vẫn đang mở/);
  for (const code of ["review_journal_unavailable", "review_closure_pending_recovery"]) {
    assert.doesNotMatch(describeError(new ApiError(503, code)), /vẫn đang mở/);
  }
});
