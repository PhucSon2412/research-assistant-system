import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { loadProxyConfig, proxyRequest } from "../lib/proxy.ts";
import {
  EMPTY_APPROVE_FORM, PUBLICATION_STATE_TEXT, REVOCATION_REASONS, STATUS_TEXT, closeReasonText,
  INDEX_ELIGIBILITY_TEXT, INDEX_EXTERNAL_NOTICE, makeIndexBody,
  fieldList, fileChoiceLabel, makeApproveBody, makePublishBody, makeReviewBody, makeRevokeBody, reviewDownloadHref,
} from "../lib/review.ts";
import { streamFromApi } from "../lib/stream.ts";

// Synthetic IDs and cookies only; nothing here is a real session or paper.
const RID = "0f1e2d3c-0000-4000-8000-000000000001";
const FID = "0f1e2d3c-0000-4000-8000-000000000002";
const file = { id: FID, version_id: "0f1e2d3c-0000-4000-8000-000000000003", filename: "paper.pdf" };
const form = { itemId: "item-1", file, acknowledged: true, noticeVersion: "2026-09-29.v1",
  source: "  Synthetic source  ", sourceUrl: " ", rights: " CC BY per requester ", licenseUrl: "https://x.test/l" };

test("review body names exactly the chosen version/file and needs the unticked box", () => {
  assert.deepEqual(makeReviewBody(form), {
    acknowledged: true, notice_version: "2026-09-29.v1", item_id: "item-1",
    version_id: file.version_id, source_file_id: FID, declared_source: "Synthetic source",
    declared_source_url: null, declared_rights: "CC BY per requester", declared_license_url: "https://x.test/l",
  });
  assert.equal(makeReviewBody({ ...form, acknowledged: false }), null);
  assert.equal(makeReviewBody({ ...form, file: undefined }), null);
  assert.equal(makeReviewBody({ ...form, noticeVersion: "" }), null);
  assert.equal(fileChoiceLabel(file, [{ id: file.version_id, version_label: "v2" }]),
    `Version v2 · paper.pdf · ${FID}`);
  assert.equal(closeReasonText("withdrawn_by_requester"), "User đã rút yêu cầu");
  assert.equal(closeReasonText("rights_not_established"), "Chưa xác lập được quyền phân phối");
  assert.equal(closeReasonText(null), null);
});

test("approve body carries only the Admin's own findings and needs the unticked confirmation", () => {
  const checksum = "a".repeat(64);
  assert.equal(EMPTY_APPROVE_FORM.confirmed, false);
  assert.deepEqual([EMPTY_APPROVE_FORM.source, EMPTY_APPROVE_FORM.licenseUrl, EMPTY_APPROVE_FORM.evidence], ["", "", ""]);
  const filled = { ...EMPTY_APPROVE_FORM, checksum, confirmed: true, title: " Bản chung ",
    source: " Synthetic source checked by Admin ", sourceUrl: " ", licenseUrl: "https://x.test/license",
    evidence: "  ", checkedOn: "2026-09-29", fulltextSearch: true, conditions: " " };
  assert.deepEqual(makeApproveBody(filled), {
    confirm_verified: true, file_checksum_sha256: checksum, title: "Bản chung",
    verified_source: "Synthetic source checked by Admin", verified_source_url: null,
    license_url: "https://x.test/license", evidence_note: null, checked_on: "2026-09-29",
    scope: { audience: "instance_users", original_download: false, fulltext_search: true,
      external_processing: false, conditions: null },
  });
  assert.equal(makeApproveBody({ ...EMPTY_APPROVE_FORM, checksum }), null);
  for (const bad of [{ confirmed: false }, { checksum: "" }, { source: "  " }, { title: "" }, { checkedOn: "" },
    { licenseUrl: "", evidence: " " }, { externalProcessing: true, fulltextSearch: false }]) {
    assert.equal(makeApproveBody({ ...filled, ...bad }), null, JSON.stringify(bad));
  }
  assert.ok(makeApproveBody({ ...filled, licenseUrl: "", evidence: "Synthetic permission letter" }));
  assert.equal(closeReasonText("approved_by_admin"), "Admin đã phê duyệt; bản chung riêng đang chờ công bố");
});

test("review download goes through the streaming route, only for the API's review path", () => {
  const path = `/admin/review-requests/${RID}/files/${FID}/original`;
  assert.equal(reviewDownloadHref(path), `/api${path}`);
  for (const bad of ["https://evil.test/x", `/files/${FID}/original`, `/admin/review-requests/${RID}/../x`,
    `//evil.test/admin/review-requests/${RID}/files/${FID}/original`]) {
    assert.equal(reviewDownloadHref(bad), null, bad);
  }
});

async function upstream(handler) {
  const seen = [];
  const server = createServer((req, res) => { seen.push({ url: req.url, headers: req.headers }); handler(req, res); });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { base: `http://127.0.0.1:${server.address().port}`, seen,
    close: () => { server.closeAllConnections(); return new Promise((resolve) => server.close(resolve)); } };
}

function browserGet(path) {
  return new Request(`http://127.0.0.1:3000/api/${path}`, { headers: {
    cookie: "ras_session=synthetic", origin: "http://127.0.0.1:3000", "x-csrf-token": "synthetic",
    authorization: "Bearer must-not-forward" } });
}

test("review original streams from the API with only the cookie and fixed errors", async () => {
  const api = await upstream((req, res) => {
    if (req.url.endsWith("/closed/files/f/original")) {
      res.writeHead(404, { "content-type": "application/json" });
      return res.end(JSON.stringify({ error: "not_found" }));
    }
    res.writeHead(200, { "content-type": "application/pdf", "content-length": "4",
      "content-disposition": "attachment; filename*=UTF-8''paper.pdf", "x-internal": "leak" });
    res.write("%P");
    setTimeout(() => res.end("DF"), 20);
  });
  try {
    const segments = ["admin", "review-requests", RID, "files", FID, "original"];
    const ok = await streamFromApi(browserGet(segments.join("/")), segments, api.base);
    assert.equal(ok.status, 200);
    assert.ok(ok.body instanceof ReadableStream);
    assert.equal(await ok.text(), "%PDF");
    assert.equal(ok.headers.get("cache-control"), "no-store");
    assert.equal(ok.headers.get("content-disposition"), "attachment; filename*=UTF-8''paper.pdf");
    assert.equal(ok.headers.get("x-internal"), null);
    const sent = api.seen.at(-1);
    assert.equal(sent.url, `/admin/review-requests/${RID}/files/${FID}/original`);
    assert.equal(sent.headers.cookie, "ras_session=synthetic");
    for (const name of ["authorization", "x-csrf-token", "origin"]) assert.equal(sent.headers[name], undefined, name);

    const closed = await streamFromApi(browserGet("x"), ["admin", "review-requests", "closed", "files", "f", "original"],
      api.base);
    assert.equal(closed.status, 404);
    await streamFromApi(browserGet("x"), ["admin", "review-requests", "a/../../files", "files", "f", "original"],
      api.base);
    assert.equal(api.seen.at(-1).url, "/admin/review-requests/a%2F..%2F..%2Ffiles/files/f/original");
  } finally {
    await api.close();
  }
  assert.equal((await streamFromApi(browserGet("x"), ["files", FID, "original"], undefined)).status, 503);
  const unreachable = await streamFromApi(browserGet("x"), ["files", FID, "original"], api.base);
  assert.deepEqual([unreachable.status, await unreachable.json()], [502, { error: "api_unreachable" }]);
});

test("JSON proxy forwards review-request JSON but never buffers an original", async () => {
  const api = await upstream((req, res) => {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ url: req.url }));
  });
  const config = loadProxyConfig({ API_INTERNAL_URL: api.base });
  try {
    for (const path of ["review-requests", "review-requests/notice", `review-requests/${RID}/withdraw`,
      "admin/review-requests", `admin/review-requests/${RID}`, `admin/review-requests/${RID}/reject`,
      `admin/review-requests/${RID}/approve`, "admin/pending-publications", `admin/pending-publications/${FID}`,
      "admin/common-copies", `admin/common-copies/${FID}`, `admin/common-copies/${FID}/publish`,
      `admin/common-copies/${FID}/revoke`]) {
      const res = await proxyRequest(browserGet(path), path.split("/"), config);
      assert.equal(res.status, 200, path);
      assert.equal(api.seen.at(-1).url, `/${path}`);
    }
    const before = api.seen.length;
    for (const path of [`admin/review-requests/${RID}/files/${FID}/original`, `uploads/original`]) {
      const res = await proxyRequest(browserGet(path), path.split("/"), config);
      assert.deepEqual([res.status, await res.json()], [404, { error: "not_found" }], path);
    }
    assert.equal(api.seen.length, before);
  } finally {
    await api.close();
  }
});

test("publish and revoke bodies need the unticked confirmation; revoke sends a code, never free text", () => {
  const checksum = "b".repeat(64);
  assert.deepEqual(makePublishBody(true, checksum), { confirm_publish: true, file_checksum_sha256: checksum });
  for (const [confirmed, sum] of [[false, checksum], [true, ""], [true, "B".repeat(64)], [true, "b".repeat(63)]]) {
    assert.equal(makePublishBody(confirmed, sum), null);
  }
  assert.deepEqual(makeRevokeBody(true, "rights_withdrawn"), { confirm_revoke: true, reason_code: "rights_withdrawn" });
  for (const [confirmed, reason] of [[false, "other"], [true, ""], [true, "because I said so"], [true, "toString"]]) {
    assert.equal(makeRevokeBody(confirmed, reason), null, `${confirmed} ${reason}`);
  }
  assert.deepEqual(Object.keys(REVOCATION_REASONS).sort(), ["duplicate_copy", "other", "rights_expired",
    "rights_recorded_in_error", "rights_withdrawn", "wrong_file_or_version"]);
  // "Listed" is metadata only; the page must never read as full text being available.
  assert.match(PUBLICATION_STATE_TEXT.listed, /toàn văn chưa khả dụng/);
  // The page's revoke body never carries a retention basis: default is removal.
  assert.equal("retain" in makeRevokeBody(true, "other"), false);
  assert.equal(fieldList(["title", "evidence_note"]), "tiêu đề, ghi chú bằng chứng");
  assert.equal(fieldList([]), "không");
  assert.equal(fieldList(null), "không");
});

test("index needs the unticked provider box and explains every refusal the API can give (M6b-2b2b)", () => {
  assert.equal(makeIndexBody(false), null);
  assert.deepEqual(makeIndexBody(true), { confirm_external_processing: true });
  assert.deepEqual(Object.keys(INDEX_ELIGIBILITY_TEXT).sort(), ["audience_not_supported", "blocked_by_conditions",
    "external_processing_not_permitted", "fulltext_not_permitted", "indexable", "not_published", "revoked",
    "rights_not_current", "unsupported_file_kind"]);
  assert.match(INDEX_ELIGIBILITY_TEXT.external_processing_not_permitted, /không tự bật tìm kiếm chỉ theo từ khóa/);
  assert.match(INDEX_EXTERNAL_NOTICE, /OpenRouter/);
  // Indexing opens search (M6b-2b3a) and RAG (M6b-2b3b) for every account; rerank and generation are outbound too.
  assert.match(INDEX_EXTERNAL_NOTICE, /mọi tài khoản tìm kiếm/);
  assert.match(INDEX_EXTERNAL_NOTICE, /rerank/);
  assert.match(INDEX_EXTERNAL_NOTICE, /Hỏi đáp \(RAG\) cũng dùng bản chung theo cùng quyền/);
  assert.match(INDEX_EXTERNAL_NOTICE, /được gửi tới provider để tạo câu trả lời/);
});

test("a close replayed after a restore keeps its decision code and never reads as a pending approval", () => {
  assert.equal(STATUS_TEXT.closed_by_recovery, "Đã đóng khi khôi phục dữ liệu");
  assert.match(closeReasonText("approved_by_admin", "closed_by_recovery"), /không có trong dữ liệu khôi phục/);
  assert.doesNotMatch(closeReasonText("approved_by_admin", "closed_by_recovery"), /đang chờ công bố/);
  assert.equal(closeReasonText("withdrawn_by_requester", "closed_by_recovery"), "User đã rút yêu cầu");
  assert.equal(closeReasonText("rights_not_established", "closed_by_recovery"), "Chưa xác lập được quyền phân phối");
});
