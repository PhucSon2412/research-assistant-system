import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { loadProxyConfig, proxyRequest } from "../lib/proxy.ts";
import {
  EMPTY_APPROVE_FORM, closeReasonText, fileChoiceLabel, makeApproveBody, makeReviewBody, reviewDownloadHref,
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
      `admin/review-requests/${RID}/approve`, "admin/pending-publications", `admin/pending-publications/${FID}`]) {
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
