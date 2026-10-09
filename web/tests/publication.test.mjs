import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, publicationError } from "../lib/api.ts";
import { DEFAULT_PUBLICATION_TIMEOUT_SECONDS, loadProxyConfig, timeoutFor } from "../lib/proxy.ts";

test("publication budget is scoped to exactly the publish POST", () => {
  const config = loadProxyConfig({ API_INTERNAL_URL: "http://api", WEB_PROXY_PUBLICATION_TIMEOUT_SECONDS: "90" });
  assert.equal(DEFAULT_PUBLICATION_TIMEOUT_SECONDS, 120);
  assert.equal(config.publicationTimeoutMs, 90_000);
  assert.equal(timeoutFor("POST", ["admin", "common-copies", "id", "publish"], config), 90_000);
  for (const [method, path] of [["GET", ["admin", "common-copies", "id", "publish"]],
    ["POST", ["admin", "common-copies", "id", "revoke"]],
    ["POST", ["admin", "common-copies", "id", "index"]],
    ["POST", ["admin", "common-copies", "publish"]],
    ["POST", ["admin", "common-copies", "id", "publish", "extra"]]]) {
    assert.equal(timeoutFor(method, path, config), 15_000);
  }
  assert.equal(timeoutFor("POST", ["admin", "review-requests", "id", "approve"], config), 120_000);
  for (const raw of ["0", "301", "-1", "1.5", "abc", "1e2", "14"]) {
    assert.equal(loadProxyConfig({ API_INTERNAL_URL: "http://api", WEB_PROXY_PUBLICATION_TIMEOUT_SECONDS: raw }), null);
  }
  assert.equal(loadProxyConfig({ API_INTERNAL_URL: "http://api", WEB_PROXY_TIMEOUT_SECONDS: "30",
    WEB_PROXY_PUBLICATION_TIMEOUT_SECONDS: "20" }), null);
  assert.equal(loadProxyConfig({ API_INTERNAL_URL: "http://api", WEB_PROXY_PUBLICATION_TIMEOUT_SECONDS: " 300 " })
    .publicationTimeoutMs, 300_000);
});

test("lost reply, timeout, incomplete body and unknown commit never claim publication failed", () => {
  for (const error of [new Error("network"), new SyntaxError("body"),
    new ApiError(503, "publication_outcome_uncertain"), new ApiError(504, "api_timeout"),
    new ApiError(502, "api_response_incomplete"), new ApiError(502, "api_unreachable"),
    new ApiError(503, "http_error")]) {
    const message = publicationError(error);
    assert.match(message, /Không xác định được kết quả công bố/);
    assert.match(message, /tải lại trạng thái/);
    assert.match(message, /không tự gửi lại/);
  }
  assert.match(publicationError(new ApiError(503, "publication_response_unavailable")), /đã lưu/);
  assert.match(publicationError(new ApiError(401, "session_expired")), /đăng nhập/);
  assert.match(publicationError(new ApiError(403, "forbidden")), /không có quyền/);
});
