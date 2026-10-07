import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, api, describeError } from "../lib/api.ts";

test("uncertain resource commit asks for checking state and never retries", async () => {
  const message = describeError(new ApiError(503, "resource_write_outcome_uncertain"));
  assert.match(message, /Chưa xác nhận.*Kiểm tra lại.*không tự gửi lại/);
  assert.doesNotMatch(message, /chưa lưu|không.*được ghi nhận/);
  const previousFetch = globalThis.fetch;
  const previousDocument = globalThis.document;
  globalThis.document = { cookie: "ras_csrf=synthetic-resource-csrf" };
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
    return new Response(JSON.stringify({ error: "resource_write_outcome_uncertain" }),
      { status: 503, headers: { "content-type": "application/json" } });
  };
  try {
    await assert.rejects(api("/collections", { method: "POST", body: { name: "Reading" } }),
      err => err instanceof ApiError && err.code === "resource_write_outcome_uncertain");
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = previousFetch;
    globalThis.document = previousDocument;
  }
  assert.match(describeError(new ApiError(503, "resource_write_response_unavailable")), /đã được lưu/);
});

test("uncertain auth transaction never claims password reset or disable succeeded", () => {
  assert.match(describeError(new ApiError(503, "auth_outcome_uncertain")), /Chưa xác định.*Kiểm tra/);
  assert.match(describeError(new ApiError(503, "session_journal_unavailable")), /Phiên đã bị thu hồi/);
  assert.doesNotMatch(describeError(new ApiError(503, "auth_journal_unavailable")), /Phiên đã bị thu hồi/);
  assert.match(describeError(new ApiError(503, "auth_closure_pending_recovery")), /Chưa xác định/);
  for (const code of ["auth_outcome_uncertain", "session_revocation_pending_recovery"]) {
    assert.doesNotMatch(describeError(new ApiError(503, code)), /đổi mật khẩu thành công|vô hiệu hóa thành công/);
  }
});

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

test("review writer uncertain and postcommit errors ask for a fresh state, never promise rollback", () => {
  const uncertain = describeError(new ApiError(503, "review_write_outcome_uncertain"));
  assert.match(uncertain, /có thể.*phục hồi.*không tự gửi lại/);
  for (const code of ["review_write_outcome_uncertain", "review_write_response_unavailable", "review_write_unavailable"]) {
    assert.doesNotMatch(describeError(new ApiError(503, code)), /vẫn đang mở|chưa lưu|Thử lại ngay/);
  }
  assert.match(describeError(new ApiError(503, "review_write_response_unavailable")), /đã lưu.*phản hồi mới/);
});

test("resource scope churn asks the reader to reload without rendering stale rows", async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ error: "resource_scope_changed" }),
    { status: 409, headers: { "content-type": "application/json" } });
  try {
    await assert.rejects(api("/library/items"), err => {
      assert.equal(err.status, 409);
      assert.match(describeError(err), /đã thay đổi.*tải lại trang/);
      return true;
    });
  } finally {
    globalThis.fetch = previousFetch;
  }
});
