import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { api, ApiError, describeError } from "../lib/api.ts";
import { proxyRequest, loadProxyConfig } from "../lib/proxy.ts";

const require = createRequire(import.meta.url);

test("upload errors distinguish uncertain commit and saved acknowledgement", () => {
  assert.match(describeError(new ApiError(503, "upload_intent_outcome_uncertain")), /Chưa xác định.*Không gửi file.*tự gửi lại/);
  assert.match(describeError(new ApiError(503, "upload_intent_response_unavailable")), /đã được lưu.*Không gửi file/);
  for (const code of ["upload_intent_changed", "upload_intent_expired", "upload_intent_outcome_uncertain", "upload_intent_response_unavailable"]) {
    assert.doesNotMatch(describeError(new ApiError(409, code)), /chưa có gì.*lưu|chưa lưu/);
  }
});

// Execute the real form callbacks with a small hook host; no DOM/React code
// change. Both api() and the proxy use actual HTTP to a synthetic upstream.
test("form/proxy smoke: unchecked means no POST; denied intent means no PUT or retry", async () => {
  const seen = [];
  let denial = "upload_intent_outcome_uncertain";
  const server = createServer((req, res) => {
    const chunks = [];
    req.on("data", c => chunks.push(c));
    req.on("end", () => {
      seen.push({ path: req.url, method: req.method, body: Buffer.concat(chunks).toString(), headers: req.headers });
      const notice = req.url === "/uploads/notice";
      res.writeHead(notice ? 200 : 503, { "content-type": "application/json" });
      res.end(JSON.stringify(notice ? { version: "synthetic.v1", text: "synthetic notice" } : { error: denial }));
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const config = loadProxyConfig({ API_INTERNAL_URL: `http://127.0.0.1:${server.address().port}` });
  const originalFetch = globalThis.fetch, originalDocument = globalThis.document;
  globalThis.document = { cookie: "ras_csrf=synthetic-csrf" };
  globalThis.fetch = async (url, init = {}) => {
    if (typeof url === "string" && url.startsWith("/api/")) {
      return proxyRequest(new Request(`http://web.test${url}`, { ...init,
        headers: { ...init.headers, Origin: "http://web.test", Cookie: "ras_session=synthetic-session; ras_csrf=synthetic-csrf" },
      }), url.slice(5).split("/"), config);
    }
    return originalFetch(url, init);
  };
  try {
    let cursor = 0, first = true;
    const states = [], effects = [];
    const hooks = {
      useState(initial) { const i = cursor++; if (!(i in states)) states[i] = initial; return [states[i], v => { states[i] = v; }]; },
      useEffect(callback) { if (first) effects.push(callback); },
    };
    const jsx = (type, props) => ({ type, props });
    const module = { exports: {} };
    const code = ts.transpileModule(readFileSync(new URL("../app/components/upload.tsx", import.meta.url), "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
    }).outputText;
    new Function("require", "module", "exports", code)(name => name === "react" ? hooks :
      name === "react/jsx-runtime" ? { jsx, jsxs: jsx } : name === "../../lib/api" ?
        { api, ApiError, describeError, csrfToken: async () => "synthetic-csrf" } : require(name), module, module.exports);
    const render = () => { cursor = 0; const tree = module.exports.Upload({ onComplete: () => assert.fail("no success") }); first = false; return tree; };
    const nodes = tree => [tree, ...[tree?.props?.children].flat(Infinity).filter(x => x && typeof x === "object").flatMap(nodes)];
    let tree = render(); effects.forEach(f => f());
    for (let i = 0; i < 100 && !states[0]; i++) await new Promise(r => setTimeout(r, 10));
    assert.ok(states[0], "notice arrived through HTTP proxy");
    tree = render();
    const find = (type, input) => nodes(tree).find(n => n.type === type && (!input || n.props.type === input));
    assert.equal(find("input", "checkbox").props.checked, false);
    find("input", "file").props.onChange({ target: { files: [new File(["# synthetic"], "synthetic.md")] } });
    tree = render();
    await find("form").props.onSubmit({ preventDefault() {} });
    assert.equal(seen.filter(r => r.method === "POST").length, 0);
    for (const error of ["upload_intent_outcome_uncertain", "upload_intent_response_unavailable"]) {
      denial = error;
      tree = render(); find("input", "checkbox").props.onChange({ target: { checked: true } });
      tree = render(); await find("form").props.onSubmit({ preventDefault() {} });
      tree = render();
      assert.equal(find("input", "checkbox").props.checked, false);
      assert.match(nodes(tree).find(n => n.props?.role === "alert").props.children, /Không gửi file/);
    }
    assert.equal(seen.filter(r => r.method === "POST").length, 2);
    assert.equal(seen.filter(r => r.method === "PUT").length, 0);
    for (const row of seen.filter(r => r.method === "POST")) {
      const body = JSON.parse(row.body);
      assert.equal(body.acknowledged, true);
      assert.equal(body.notice_version, "synthetic.v1");
      assert.equal("file" in body, false);
      assert.equal(row.headers.origin, "http://web.test");
      assert.equal(row.headers["x-csrf-token"], "synthetic-csrf");
    }
  } finally {
    globalThis.fetch = originalFetch; globalThis.document = originalDocument;
    server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
  }
});
