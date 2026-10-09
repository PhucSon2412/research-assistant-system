import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, revocationError } from "../lib/api.ts";
import { loadProxyConfig, timeoutFor } from "../lib/proxy.ts";

test("revoke retains the normal exact POST budget", () => {
  const config = loadProxyConfig({ API_INTERNAL_URL: "http://api" });
  assert.equal(timeoutFor("POST", ["admin", "common-copies", "id", "revoke"], config), 15_000);
});

test("revoke lost response never implies rollback or automatic retry", () => {
  for (const error of [new Error("network"), new SyntaxError("body"),
    new ApiError(503, "revocation_outcome_uncertain"), new ApiError(504, "api_timeout"),
    new ApiError(502, "api_response_incomplete"), new ApiError(502, "api_unreachable"), new ApiError(503, "http_error")]) {
    const message = revocationError(error);
    assert.match(message, /Không xác định được kết quả thu hồi/);
    assert.match(message, /tải lại trạng thái/);
    assert.match(message, /không tự gửi lại/);
  }
  assert.match(revocationError(new ApiError(503, "revocation_journal_unavailable")), /có thể đã bị đóng/);
  assert.match(revocationError(new ApiError(503, "revocation_journal_unavailable")), /recovery/);
  assert.match(revocationError(new ApiError(503, "revocation_response_unavailable")), /đã lưu/);
  assert.match(revocationError(new ApiError(401, "session_expired")), /đăng nhập/);
});
