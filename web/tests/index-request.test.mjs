import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, indexRequestError } from "../lib/api.ts";
import { loadProxyConfig, timeoutFor } from "../lib/proxy.ts";
import { isIndexRequestResult, makeIndexBody } from "../lib/review.ts";

test("index request keeps the normal exact POST budget (DB only, no storage/provider)", () => {
  const config = loadProxyConfig({ API_INTERNAL_URL: "http://api", WEB_PROXY_PUBLICATION_TIMEOUT_SECONDS: "200",
    WEB_PROXY_REVIEW_APPROVAL_TIMEOUT_SECONDS: "200" });
  assert.equal(timeoutFor("POST", ["admin", "common-copies", "id", "index"], config), 15_000);
});

test("only an explicit tick produces a body", () => {
  assert.equal(makeIndexBody(false), null);
  assert.deepEqual(makeIndexBody(true), { confirm_external_processing: true });
});

test("lost reply, proxy timeout and unknown commit never claim no job was created", () => {
  for (const error of [new Error("network"), new SyntaxError("body"),
    new ApiError(503, "index_start_outcome_uncertain"), new ApiError(504, "api_timeout"),
    new ApiError(502, "api_response_incomplete"), new ApiError(502, "api_unreachable"), new ApiError(503, "http_error")]) {
    const message = indexRequestError(error);
    assert.match(message, /Không xác định được kết quả yêu cầu lập chỉ mục/);
    assert.match(message, /job có thể đã được tạo/);
    assert.match(message, /tải lại trạng thái/);
    assert.match(message, /không tự gửi lại/);
  }
  assert.match(indexRequestError(new ApiError(503, "index_start_response_unavailable")), /đã được lưu/);
  assert.match(indexRequestError(new ApiError(503, "index_start_unavailable")), /không có job mới/);
  assert.match(indexRequestError(new ApiError(409, "index_job_cancelling")), /yêu cầu hủy/);
  assert.match(indexRequestError(new ApiError(401, "session_expired")), /đăng nhập/);
  assert.match(indexRequestError(new ApiError(403, "forbidden")), /không có quyền/);
});

test("a success body must name this copy and a durable job; created is not indexed", () => {
  const ok = { item: { id: "c" }, job: { id: "j", state: "received", cancel_requested: false }, created: true };
  assert.equal(isIndexRequestResult(ok, "c"), true);
  assert.equal(isIndexRequestResult(ok, "other"), false);
  for (const broken of [null, {}, { ...ok, created: "true" }, { ...ok, job: { id: "j", state: "received" } },
    { item: ok.item, created: false }]) {
    assert.equal(isIndexRequestResult(broken, "c"), false);
  }
});
