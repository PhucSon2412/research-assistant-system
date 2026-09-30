import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import {
  COMMON_DOWNLOAD_NOTE, COMMON_INDEXED_NOTE, COMMON_METADATA_ONLY_NOTE, COMMON_SEARCHABLE_DOWNLOAD_NOTE,
  COMMON_SEARCHABLE_NOTE, NO_DOWNLOADABLE_FILE, commonItemNote, contentLabel, originalDownloadHref,
  sourceOriginalHref,
} from "../lib/files.ts";
import { CONDITIONS_BLOCK_NOTE, ORIGINAL_DOWNLOAD_TEXT } from "../lib/review.ts";
import { streamFromApi } from "../lib/stream.ts";

// Synthetic IDs, cookies and bytes only (M6b-2b1).
const FID = "0f1e2d3c-0000-4000-8000-00000000000f";
const listed = { id: FID, version_id: FID, filename: "paper.md", byte_size: 4, verified_mime: "text/markdown",
  page_count: null, original_url: `/files/${FID}/original` };

test("download link comes only from the API's own file path, through the streaming route", () => {
  assert.equal(originalDownloadHref(`/files/${FID}/original`), `/api/files/${FID}/original`);
  for (const bad of [undefined, null, "", `https://minio.test/originals/${FID}`, `//evil.test/files/${FID}/original`,
    `/files/${FID}/original?x=1`, `/files/${FID}/../original`, `/files/not-a-uuid/original`,
    `/admin/review-requests/${FID}/files/${FID}/original`, `/api/files/${FID}/original`,
    `/files/${FID.toUpperCase()}/original`]) {
    assert.equal(originalDownloadHref(bad), null, String(bad));
  }
});

test("a common item's note depends only on whether the API listed a file", () => {
  assert.equal(commonItemNote([]), COMMON_METADATA_ONLY_NOTE);
  assert.equal(commonItemNote([listed]), COMMON_DOWNLOAD_NOTE);
  assert.match(COMMON_METADATA_ONLY_NOTE, /^Toàn văn chưa khả dụng/);
  assert.match(COMMON_METADATA_ONLY_NOTE, /Không có file gốc được phép tải/);
  // Downloading the original is not full text: search/chunks/RAG stay closed.
  assert.match(COMMON_DOWNLOAD_NOTE, /Toàn văn chưa khả dụng/);
  assert.match(COMMON_DOWNLOAD_NOTE, /SHA-256/);
  assert.match(NO_DOWNLOADABLE_FILE, /Không có file gốc/);
  assert.match(CONDITIONS_BLOCK_NOTE, /không mở tải/);
  assert.deepEqual(Object.keys(ORIGINAL_DOWNLOAD_TEXT).sort(),
    ["blocked_by_conditions", "not_permitted", "not_published", "open", "revoked", "rights_not_current"]);
});

test("an indexed common item is searchable only when the API says so (M6b-2b3a)", () => {
  // Indexed without the API's flag: closed, whatever the files.
  assert.equal(commonItemNote([], "indexed"), COMMON_INDEXED_NOTE);
  assert.equal(commonItemNote([listed], "indexed", false), COMMON_INDEXED_NOTE);
  assert.equal(commonItemNote([listed], "metadata_only"), COMMON_DOWNLOAD_NOTE);
  assert.match(COMMON_INDEXED_NOTE, /^Đã lập chỉ mục, chưa mở tìm kiếm/);
  // Searchable: the download part still depends only on the files the API listed.
  assert.equal(commonItemNote([], "indexed", true), COMMON_SEARCHABLE_NOTE);
  assert.equal(commonItemNote([listed], "indexed", true), COMMON_SEARCHABLE_DOWNLOAD_NOTE);
  assert.match(COMMON_SEARCHABLE_NOTE, /không mở tải file gốc/);
  for (const note of [COMMON_SEARCHABLE_NOTE, COMMON_SEARCHABLE_DOWNLOAD_NOTE]) {
    assert.match(note, /^Tìm kiếm toàn văn khả dụng/);
    assert.match(note, /Hỏi đáp \(RAG\) chưa dùng bản chung/);
  }
});

test("content labels come from the API's full-text flag, never from content_status alone", () => {
  assert.equal(contentLabel({ visibility: "common", content_status: "indexed", fulltext_search_available: true }),
    "Bản chung: tìm kiếm toàn văn (chưa hỏi đáp)");
  // Indexed common copy the gate does not serve (e.g. rights no longer current): no full-text label.
  assert.equal(contentLabel({ visibility: "common", content_status: "indexed", fulltext_search_available: false }),
    "Đã lập chỉ mục, chưa mở tìm kiếm");
  assert.equal(contentLabel({ visibility: "common", content_status: "indexed" }), "Đã lập chỉ mục, chưa mở tìm kiếm");
  assert.equal(contentLabel({ visibility: "common", content_status: "metadata_only", fulltext_search_available: false }),
    "Chỉ metadata");
  assert.equal(contentLabel({ visibility: "private", content_status: "indexed", fulltext_search_available: true }),
    "Đã index");
  assert.equal(contentLabel({ visibility: "private", content_status: "metadata_only" }), "Chỉ metadata");
});

test("a hit's original link needs the API's download path; search rights never build one", () => {
  assert.equal(sourceOriginalHref(null, "pdf", 3), null);
  assert.equal(sourceOriginalHref(undefined, "markdown", null), null);
  assert.equal(sourceOriginalHref(`https://minio.test/originals/${FID}`, "pdf", 1), null);
  assert.equal(sourceOriginalHref(`/files/${FID}/original`, "pdf", 3), `/api/files/${FID}/original#page=3`);
  assert.equal(sourceOriginalHref(`/files/${FID}/original`, "markdown", null), `/api/files/${FID}/original`);
});

async function upstream(handler) {
  const seen = [];
  const server = createServer((req, res) => { seen.push({ url: req.url, headers: req.headers }); handler(req, res); });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { base: `http://127.0.0.1:${server.address().port}`, seen,
    close: () => { server.closeAllConnections(); return new Promise((resolve) => server.close(resolve)); } };
}

function browserGet() {
  return new Request(`http://127.0.0.1:3000/api/files/${FID}/original`, { headers: {
    cookie: "ras_session=synthetic", range: "bytes=0-1", "x-csrf-token": "synthetic" } });
}

test("common original streams with only the cookie; API errors pass through with no file bytes", async () => {
  const answers = {
    ok: [200, { "content-type": "text/markdown", "content-length": "4",
      "content-disposition": "attachment; filename*=UTF-8''paper.md", "cache-control": "no-store" }, "# ok"],
    missing: [404, { "content-type": "application/json" }, JSON.stringify({ error: "not_found" })],
    integrity: [409, { "content-type": "application/json" }, JSON.stringify({ error: "common_file_integrity_failed" })],
    busy: [503, { "content-type": "application/json" }, JSON.stringify({ error: "download_busy" })],
  };
  let answer = "ok";
  const api = await upstream((req, res) => {
    const [status, headers, body] = answers[answer];
    res.writeHead(status, headers);
    res.end(body);
  });
  try {
    const ok = await streamFromApi(browserGet(), ["files", FID, "original"], api.base);
    assert.equal(ok.status, 200);
    assert.ok(ok.body instanceof ReadableStream);
    assert.equal(await ok.text(), "# ok");
    assert.equal(ok.headers.get("cache-control"), "no-store");
    assert.equal(ok.headers.get("x-content-type-options"), "nosniff");
    const sent = api.seen.at(-1);
    assert.equal(sent.url, `/files/${FID}/original`);
    assert.equal(sent.headers.cookie, "ras_session=synthetic");
    // No Range is forwarded: every request downloads the whole verified file or nothing.
    for (const name of ["range", "x-csrf-token"]) assert.equal(sent.headers[name], undefined, name);

    for (const [name, error] of [["missing", "not_found"], ["integrity", "common_file_integrity_failed"],
      ["busy", "download_busy"]]) {
      answer = name;
      const refused = await streamFromApi(browserGet(), ["files", FID, "original"], api.base);
      assert.equal(refused.status, answers[name][0], name);
      assert.deepEqual(await refused.json(), { error }, name);
      assert.equal(refused.headers.get("content-disposition"), null, name);
      assert.equal(refused.headers.get("cache-control"), "no-store", name);
    }
  } finally {
    await api.close();
  }
});
