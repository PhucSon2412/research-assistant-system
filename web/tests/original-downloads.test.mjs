import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { streamFromApi } from "../lib/stream.ts";

test("two download branches stream before EOF, forward cookie only and keep denial cookies", async () => {
  for (const segments of [["files", "file-id", "original"], ["admin", "review-requests", "request-id", "files", "file-id", "original"]]) {
    let finish;
    const wait = new Promise(resolve => { finish = resolve; });
    const seen = [];
    const server = createServer((req, res) => {
      seen.push(req.url);
      assert.equal(req.headers.cookie, "ras_session=synthetic");
      assert.equal(req.headers["x-csrf-token"], undefined);
      if (req.url.includes("denied")) {
        res.writeHead(401, { "content-type": "application/json", "set-cookie": ["ras_session=; Max-Age=0", "ras_csrf=; Max-Age=0"] });
        res.end('{"error":"session_expired"}');
      } else {
        res.writeHead(200, { "content-type": "text/markdown", "content-length": "11", "content-disposition": "attachment; filename=synthetic.md" });
        res.write("first"); wait.then(() => res.end("second"));
      }
    });
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    const base = `http://127.0.0.1:${server.address().port}`;
    try {
      const request = new Request("http://web.test/api/file", { headers: { Cookie: "ras_session=synthetic", "X-CSRF-Token": "do-not-forward" } });
      const response = await streamFromApi(request, segments, base);
      const reader = response.body.getReader();
      assert.equal(new TextDecoder().decode((await reader.read()).value), "first");
      finish();
      assert.equal(new TextDecoder().decode((await reader.read()).value), "second");
      assert.equal((await reader.read()).done, true);
      assert.equal(response.headers.get("cache-control"), "no-store");
      assert.equal(response.headers.get("x-content-type-options"), "nosniff");
      const denial = await streamFromApi(request, segments.map(s => s === "file-id" ? "denied" : s), base);
      assert.equal(denial.status, 401); assert.equal(denial.headers.getSetCookie().length, 2);
      assert.equal(denial.headers.get("content-disposition"), null);
      assert.deepEqual(await denial.json(), { error: "session_expired" });
      assert.equal(seen.length, 2);
    } finally { finish(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
  }
});

for (const mode of ["body-cancel", "request-abort", "preheaders-abort", "incomplete"]) {
  test(`download ${mode} ends upstream without retry or successful partial body`, async () => {
    let disconnected;
    const closed = new Promise(resolve => { disconnected = resolve; });
    let calls = 0;
    const server = createServer((req, res) => {
      calls++; res.on("close", disconnected);
      if (mode === "preheaders-abort") return;
      res.writeHead(200, { "content-length": "1000000", "content-type": "application/pdf" });
      res.write("first");
      if (mode === "incomplete") setImmediate(() => res.destroy());
    });
    await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
    try {
      const controller = new AbortController();
      const request = new Request("http://web.test/api/file", { signal: controller.signal });
      const result = streamFromApi(request, ["files", "id", "original"], `http://127.0.0.1:${server.address().port}`);
      if (mode === "preheaders-abort") {
        // A local request event makes cancellation deterministic after upstream acceptance.
        await new Promise(resolve => {
          const check = () => calls ? resolve() : setImmediate(check); check();
        });
        controller.abort(); assert.equal((await result).status, 502);
      } else {
        const response = await result;
        if (mode === "body-cancel") await response.body.cancel();
        else if (mode === "request-abort") { controller.abort(); await assert.rejects(response.arrayBuffer()); }
        else await assert.rejects(response.arrayBuffer());
      }
      await closed; assert.equal(calls, 1);
    } finally { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
  });
}

test("download failures preserve 404/409/503 without attachment headers", async () => {
  const real = globalThis.fetch;
  try {
    for (const [status, error] of [[404, "not_found"], [409, "private_file_integrity_failed"], [503, "download_busy"]]) {
      let calls = 0;
      globalThis.fetch = async () => { calls++; return Response.json({ error }, { status }); };
      const r = await streamFromApi(new Request("http://web.test"), ["files", "id", "original"], "http://local.test");
      assert.equal(r.status, status); assert.deepEqual(await r.json(), { error });
      assert.equal(r.headers.get("content-disposition"), null); assert.equal(calls, 1);
    }
  } finally { globalThis.fetch = real; }
});
