import assert from "node:assert/strict";
import test from "node:test";
import { ApiError } from "../lib/api.ts";
import { deletionError, deletionStatus } from "../lib/private-delete.ts";

test("private deletion timeout, fsync and uncertain outcomes never imply rollback", () => {
  for (const error of [new Error("network"), new SyntaxError("body"),
    new ApiError(503, "deletion_outcome_uncertain"), new ApiError(503, "deletion_journal_unavailable"),
    new ApiError(503, "deletion_response_unavailable"), new ApiError(504, "api_timeout")]) {
    assert.match(deletionError(error), /Chưa xác định/);
    assert.match(deletionError(error), /không tự gửi lại/);
  }
});

test("logical delete receipt does not claim physical purge", () => {
  assert.match(deletionStatus({ cleanup_status: "pending", purge_due_at: "2099" }), /đang chờ cleanup/);
  assert.match(deletionStatus({ cleanup_status: "completed" }), /hoàn tất cleanup/);
});
