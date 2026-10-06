import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { PUT } from "../app/api/uploads/original/route.ts";
import { api, ApiError, describeError } from "../lib/api.ts";

test("upload receipt errors never promise a failed mutation or automatic retry", () => {
  for (const code of ["upload_outcome_uncertain", "upload_response_unavailable"]) {
    const text = describeError(new ApiError(503, code));
    assert.match(text, /kiểm tra thư viện|Kiểm tra thư viện/);
    assert.match(text, /Không tự gửi lại|không tự gửi lại/);
  }
});

test("dedicated PUT proxy streams before EOF and preserves auth denial cookies", async () => {
  let first;
  const arrived = new Promise(r => { first = r; });
  const chunks = [];
  const server = createServer((req, res) => {
    req.on("data", chunk => { chunks.push(chunk); first(); });
    req.on("end", () => {
      assert.equal(req.headers["x-upload-intent"], "synthetic-intent");
      assert.equal(req.headers["x-csrf-token"], "synthetic-csrf");
      assert.equal(req.headers.origin, "http://web.test");
      res.writeHead(401, { "content-type": "application/json", "set-cookie": ["ras_session=; Max-Age=0", "ras_csrf=; Max-Age=0"] });
      res.end(JSON.stringify({ error: "session_expired" }));
    });
  });
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  const old = process.env.API_INTERNAL_URL;
  process.env.API_INTERNAL_URL = `http://127.0.0.1:${server.address().port}`;
  try {
    let controller;
    const body = new ReadableStream({ start(c) { controller = c; c.enqueue(new TextEncoder().encode("# first\n")); } });
    const result = PUT(new Request("http://web.test/api/uploads/original", { method: "PUT", body, duplex: "half",
      headers: { Origin: "http://web.test", Cookie: "ras_session=synthetic", "X-CSRF-Token": "synthetic-csrf", "X-Upload-Intent": "synthetic-intent", "Content-Type": "application/octet-stream" } }));
    await arrived; // upstream receives bytes while client body is STILL OPEN
    controller.enqueue(new TextEncoder().encode("second")); controller.close();
    const response = await result;
    assert.equal(response.status, 401);
    assert.deepEqual(await response.json(), { error: "session_expired" });
    assert.equal(Buffer.concat(chunks).toString(), "# first\nsecond");
    assert.equal(response.headers.getSetCookie().length, 2);
    assert.equal(response.headers.get("cache-control"), "no-store");
  } finally {
    if (old === undefined) delete process.env.API_INTERNAL_URL; else process.env.API_INTERNAL_URL = old;
    server.closeAllConnections(); await new Promise(r => server.close(r));
  }
});

test("proxy lost response is uncertain and does not replay PUT", async () => {
  const old = globalThis.fetch, oldBase = process.env.API_INTERNAL_URL;
  let calls = 0;
  process.env.API_INTERNAL_URL = "http://127.0.0.1:1";
  globalThis.fetch = async () => { calls++; throw new Error("synthetic lost response"); };
  try {
    const r = await PUT(new Request("http://web.test/api/uploads/original", { method: "PUT", body: "synthetic" }));
    assert.equal(r.status, 502); assert.equal(calls, 1);
    assert.deepEqual(await r.json(), { error: "upload_outcome_uncertain" });
  } finally { globalThis.fetch = old; if (oldBase === undefined) delete process.env.API_INTERNAL_URL; else process.env.API_INTERNAL_URL = oldBase; }
});

test("real form callbacks: intent precedes file, unknown PUT outcome has no IDs or retry", async () => {
  const require = createRequire(import.meta.url);
  const oldFetch = globalThis.fetch, oldDocument = globalThis.document;
  globalThis.document = { cookie: "ras_csrf=synthetic-csrf" };
  const seen = []; let mode = "lost";
  globalThis.fetch = async (url, init = {}) => {
    seen.push({ url, method: init.method ?? "GET", body: init.body });
    if (url.endsWith("/notice")) return Response.json({ version: "test.v1", text: "synthetic notice" });
    if (url.endsWith("/intents")) return Response.json({ intent_token: "synthetic-intent" }, { status: 201 });
    if (mode === "lost") throw new Error("synthetic network error");
    if (mode === "proxy") return new Response("proxy gateway failure", { status: 504 });
    if (mode === "missing") return Response.json({}, { status: 201 });
    if (mode === "denied") return Response.json({ error: "session_expired" }, { status: 401 });
    return Response.json({ item_id: "synthetic-item", state: "received" }, { status: 201 });
  };
  try {
    let cursor = 0, first = true, completed = 0;
    const states = [], effects = [];
    const hooks = {
      useState(initial) { const i = cursor++; if (!(i in states)) states[i] = initial; return [states[i], v => { states[i] = v; }]; },
      useEffect(cb) { if (first) effects.push(cb); },
    };
    const jsx = (type, props) => ({ type, props });
    const module = { exports: {} };
    const code = ts.transpileModule(readFileSync(new URL("../app/components/upload.tsx", import.meta.url), "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
    }).outputText;
    new Function("require", "module", "exports", code)(name => name === "react" ? hooks :
      name === "react/jsx-runtime" ? { jsx, jsxs: jsx } : name === "../../lib/api" ?
        { api, ApiError, describeError, csrfToken: async () => "synthetic-csrf" } : require(name), module, module.exports);
    const render = () => { cursor = 0; const tree = module.exports.Upload({ onComplete: () => { completed++; } }); first = false; return tree; };
    const nodes = tree => [tree, ...[tree?.props?.children].flat(Infinity).filter(x => x && typeof x === "object").flatMap(nodes)];
    let tree = render(); effects.forEach(f => f()); await new Promise(r => setImmediate(r));
    const find = (type, input) => nodes(tree).find(n => n.type === type && (!input || n.props.type === input));
    tree = render(); find("input", "file").props.onChange({ target: { files: [new File(["# synthetic"], "test.md")] } });
    for (const result of ["lost", "proxy", "missing", "denied", "success"]) {
      mode = result; tree = render(); find("input", "checkbox").props.onChange({ target: { checked: true } });
      tree = render(); await find("form").props.onSubmit({ preventDefault() {} }); tree = render();
      assert.equal(find("input", "checkbox").props.checked, false);
      if (result !== "success") {
        assert.match(nodes(tree).find(n => n.props?.role === "alert").props.children, /thư viện/);
        assert.equal(completed, 0);
        assert.equal(nodes(tree).filter(n => n.type === "a").length, 0);
      }
    }
    assert.equal(completed, 1);
    const operations = seen.filter(s => s.method !== "GET");
    assert.deepEqual(operations.map(s => s.method), Array(5).fill(["POST", "PUT"]).flat());
    for (const op of operations.filter(s => s.method === "POST")) assert.equal("file" in JSON.parse(op.body), false);
    for (const op of operations.filter(s => s.method === "PUT")) assert.ok(op.body instanceof File);
  } finally { globalThis.fetch = oldFetch; globalThis.document = oldDocument; }
});
