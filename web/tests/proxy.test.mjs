import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import {
  DEFAULT_PROVIDER_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS, loadProxyConfig, proxyRequest, timeoutFor,
} from "../lib/proxy.ts";

// Synthetic upstream standing in for FastAPI; values below are fake, not real sessions.
const COOKIE = "ras_session=synthetic-session; ras_csrf=synthetic-csrf";
const ORIGIN = "http://127.0.0.1:3000";

test("proxy preserves real Origin and never synthesises missing Origin", async () => {
  const api = await upstream(echo);
  const config = loadProxyConfig({ API_INTERNAL_URL: api.base });
  try {
    for (const origin of [undefined, "null", "https://evil.test", ORIGIN]) {
      const headers = { cookie: COOKIE, "x-csrf-token": "synthetic-csrf", referer: `${ORIGIN}/login` };
      if (origin !== undefined) headers.origin = origin;
      await proxyRequest(new Request(`${ORIGIN}/api/auth/login`, { method: "POST", headers, body: "{}" }), ["auth", "login"], config);
      assert.equal(api.seen.at(-1).headers.origin, origin);
      assert.equal(api.seen.at(-1).headers.referer, undefined);
    }
  } finally { await api.close(); }
});

async function upstream(handler) {
  const seen = [];
  const server = createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      seen.push({ method: req.method, url: req.url, headers: req.headers, body: Buffer.concat(chunks).toString() });
      handler(req, res);
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  return {
    base, seen,
    close: () => { server.closeAllConnections(); return new Promise((resolve) => server.close(resolve)); },
  };
}

function browser(method, path, body) {
  return new Request(`${ORIGIN}/api/${path}`, {
    method,
    headers: {
      cookie: COOKIE, origin: ORIGIN, accept: "application/json", "x-csrf-token": "synthetic-csrf",
      ...(body === undefined ? {} : { "content-type": "application/json" }),
      authorization: "Bearer must-not-forward", "x-forwarded-for": "10.0.0.1",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function segments(path) {
  return path.split("?")[0].split("/");
}

const echo = (req, res) => {
  const status = req.method === "DELETE" ? 204 : req.method === "POST" ? 201 : 200;
  res.writeHead(status, {
    "content-type": "application/json", "cache-control": "no-store",
    "set-cookie": ["ras_csrf=rotated; Path=/; SameSite=Lax"], "x-internal-debug": "leak", server: "uvicorn",
  });
  res.end(status === 204 ? undefined : JSON.stringify({ ok: true, method: req.method, url: req.url }));
};

test("search, collections and RAG routes are forwarded intact", async () => {
  const api = await upstream(echo);
  const config = loadProxyConfig({ API_INTERNAL_URL: api.base });
  const cases = [
    ["POST", "search?trace=0", { query: "attention", mode: "keyword" }],
    ["GET", "search/chunks/0f1e2d3c-0000-4000-8000-000000000001", undefined],
    ["GET", "collections?limit=20&offset=40", undefined],
    ["POST", "collections", { name: "Reading list" }],
    ["PATCH", "collections/c-1", { name: "Renamed" }],
    ["GET", "collections/c-1/items?limit=100", undefined],
    ["POST", "collections/c-1/items", { item_id: "i-1" }],
    ["DELETE", "collections/c-1/items/i-1", undefined],
    ["DELETE", "collections/c-1", undefined],
    ["POST", "rag/ask", { scope: "all", question: "Q?", mode: "keyword" }],
  ];
  try {
    for (const [method, path, body] of cases) {
      const res = await proxyRequest(browser(method, path, body), segments(path), config);
      const got = api.seen.at(-1);
      assert.equal(got.method, method, path);
      assert.equal(got.url, `/${path}`, path);
      assert.equal(got.body, body === undefined ? "" : JSON.stringify(body), path);
      assert.equal(got.headers.cookie, COOKIE);
      assert.equal(got.headers["x-csrf-token"], "synthetic-csrf");
      assert.equal(got.headers.origin, ORIGIN);
      assert.equal(got.headers.accept, "application/json");
      if (body !== undefined) assert.equal(got.headers["content-type"], "application/json");
      assert.equal(got.headers.authorization, undefined);
      assert.equal(got.headers["x-forwarded-for"], undefined);

      const expected = method === "DELETE" ? 204 : method === "POST" ? 201 : 200;
      assert.equal(res.status, expected, path);
      assert.deepEqual(res.headers.getSetCookie(), ["ras_csrf=rotated; Path=/; SameSite=Lax"]);
      assert.equal(res.headers.get("x-internal-debug"), null);
      assert.equal(res.headers.get("server"), null);
      if (expected === 204) {
        assert.equal(await res.text(), "");
      } else {
        assert.equal(res.headers.get("content-type"), "application/json");
        assert.equal(res.headers.get("cache-control"), "no-store");
        assert.deepEqual(await res.json(), { ok: true, method, url: `/${path}` });
      }
    }
  } finally {
    await api.close();
  }
});

test("API error statuses and bodies pass through unchanged", async () => {
  const api = await upstream((req, res) => {
    const [status, error] = req.url === "/rag/ask" ? [503, "rag_generation_unavailable_unverified_pricing"]
      : req.url.startsWith("/search/chunks/") ? [404, "not_found"] : [403, "csrf_failed"];
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify({ error }));
  });
  const config = loadProxyConfig({ API_INTERNAL_URL: api.base });
  try {
    const rag = await proxyRequest(browser("POST", "rag/ask", { item_id: "x", question: "Q" }), ["rag", "ask"], config);
    assert.equal(rag.status, 503);
    assert.deepEqual(await rag.json(), { error: "rag_generation_unavailable_unverified_pricing" });
    const chunk = await proxyRequest(browser("GET", "search/chunks/other-user"), ["search", "chunks", "other-user"], config);
    assert.equal(chunk.status, 404);
    assert.deepEqual(await chunk.json(), { error: "not_found" });
    const csrf = await proxyRequest(browser("POST", "collections", { name: "x" }), ["collections"], config);
    assert.equal(csrf.status, 403);
    assert.deepEqual(await csrf.json(), { error: "csrf_failed" });
  } finally {
    await api.close();
  }
});

test("prefixes outside the allowlist and dot segments never reach the API", async () => {
  const api = await upstream(echo);
  const config = loadProxyConfig({ API_INTERNAL_URL: api.base });
  const blocked = [
    [], ["readyz"], ["healthz"], ["docs"], ["openapi.json"], ["files", "f-1", "original"],
    ["Search"], ["rag.ask"], ["search", "..", "readyz"], ["collections", ".", "x"],
    ["rag", "..%2F..%2Freadyz"].map(decodeURIComponent), ["search", "a\\..\\readyz"], ["search", ""],
  ];
  try {
    for (const path of blocked) {
      const res = await proxyRequest(browser("GET", path.join("/") || ""), path, config);
      assert.equal(res.status, 404, JSON.stringify(path));
      assert.deepEqual(await res.json(), { error: "not_found" });
    }
    assert.equal(api.seen.length, 0);
  } finally {
    await api.close();
  }
});

test("timeout config: defaults, overrides and invalid values fail closed", () => {
  const base = "http://api:8000";
  assert.deepEqual(loadProxyConfig({ API_INTERNAL_URL: base }),
    { base, timeoutMs: DEFAULT_TIMEOUT_SECONDS * 1000, providerTimeoutMs: DEFAULT_PROVIDER_TIMEOUT_SECONDS * 1000 });
  assert.equal(DEFAULT_TIMEOUT_SECONDS, 15);
  assert.equal(DEFAULT_PROVIDER_TIMEOUT_SECONDS, 120);
  assert.deepEqual(loadProxyConfig({ API_INTERNAL_URL: `${base}/`, WEB_PROXY_TIMEOUT_SECONDS: "20",
    WEB_PROXY_PROVIDER_TIMEOUT_SECONDS: " 300 " }), { base, timeoutMs: 20_000, providerTimeoutMs: 300_000 });
  for (const [timeout, provider] of [["0", "120"], ["abc", "120"], ["15", "301"], ["15", "1.5"],
    ["15", "-5"], ["30", "20"], ["15", "1e2"]]) {
    assert.equal(loadProxyConfig({ API_INTERNAL_URL: base, WEB_PROXY_TIMEOUT_SECONDS: timeout,
      WEB_PROXY_PROVIDER_TIMEOUT_SECONDS: provider }), null, `${timeout}/${provider}`);
  }
  assert.equal(loadProxyConfig({}), null);
});

test("only provider-bound routes get the longer budget", () => {
  const config = { base: "http://api", timeoutMs: 15_000, providerTimeoutMs: 120_000 };
  assert.equal(timeoutFor("POST", ["rag", "ask"], config), 120_000);
  assert.equal(timeoutFor("POST", ["search"], config), 120_000);
  assert.equal(timeoutFor("GET", ["search", "chunks", "c-1"], config), 15_000);
  assert.equal(timeoutFor("POST", ["collections"], config), 15_000);
  assert.equal(timeoutFor("GET", ["rag", "ask"], config), 15_000);
  assert.equal(timeoutFor("POST", ["library", "items"], config), 15_000);
});

test("misconfigured proxy answers 503 without contacting the API", async () => {
  const res = await proxyRequest(browser("POST", "rag/ask", {}), ["rag", "ask"], null);
  assert.equal(res.status, 503);
  assert.deepEqual(await res.json(), { error: "api_not_configured" });
});

test("RAG answer arriving after 15 s is delivered in full; a normal route still stops at 15 s", async () => {
  const answer = JSON.stringify({ status: "answered", claims: [{ text: "x".repeat(50_000) }] });
  const api = await upstream((req, res) => {
    setTimeout(() => {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(req.url === "/rag/ask" ? answer : "{}");
    }, 16_000);
  });
  const config = loadProxyConfig({ API_INTERNAL_URL: api.base }); // shipped defaults: 15 s / 120 s
  try {
    const started = Date.now();
    const [rag, slow] = await Promise.all([
      proxyRequest(browser("POST", "rag/ask", { scope: "all", question: "Q" }), ["rag", "ask"], config),
      proxyRequest(browser("GET", "collections"), ["collections"], config),
    ]);
    assert.equal(rag.status, 200);
    assert.equal(await rag.text(), answer);
    assert.ok(Date.now() - started >= 16_000);
    assert.equal(slow.status, 504);
    assert.deepEqual(await slow.json(), { error: "api_timeout" });
  } finally {
    await api.close();
  }
});

const FAST = { timeoutMs: 200, providerTimeoutMs: 400 };

test("no headers before the deadline returns a bounded 504", async () => {
  const api = await upstream(() => {}); // never answers
  try {
    const started = Date.now();
    const res = await proxyRequest(browser("POST", "rag/ask", { scope: "all", question: "Q" }), ["rag", "ask"],
      { base: api.base, ...FAST });
    const elapsed = Date.now() - started;
    assert.equal(res.status, 504);
    assert.deepEqual(await res.json(), { error: "api_timeout" });
    assert.equal(res.headers.get("cache-control"), "no-store");
    assert.ok(elapsed >= 400 && elapsed < 3_000, `elapsed ${elapsed}`);
  } finally {
    await api.close();
  }
});

test("body stalling after headers hits the same deadline", async () => {
  const api = await upstream((req, res) => {
    res.writeHead(200, { "content-type": "application/json", "content-length": "1000" });
    res.write('{"status":"answered","claims":[{"text":"synthetic partial');
  });
  try {
    const started = Date.now();
    const res = await proxyRequest(browser("POST", "rag/ask", { scope: "all", question: "Q" }), ["rag", "ask"],
      { base: api.base, ...FAST });
    assert.equal(res.status, 504);
    assert.deepEqual(await res.json(), { error: "api_timeout" });
    assert.ok(Date.now() - started < 3_000);
  } finally {
    await api.close();
  }
});

test("connection dropped mid-body returns 502 without partial data", async () => {
  const api = await upstream((req, res) => {
    res.writeHead(200, { "content-type": "application/json", "content-length": "1000" });
    res.write('{"status":"answered","claims":[{"text":"synthetic partial', () => res.socket.destroy());
  });
  try {
    const res = await proxyRequest(browser("POST", "rag/ask", { scope: "all", question: "Q" }), ["rag", "ask"],
      { base: api.base, ...FAST });
    assert.equal(res.status, 502);
    const text = await res.text();
    assert.deepEqual(JSON.parse(text), { error: "api_response_incomplete" });
    assert.ok(!text.includes("synthetic partial"));
  } finally {
    await api.close();
  }
});

test("unreachable API returns 502 without internal details", async () => {
  const api = await upstream(echo);
  const base = api.base;
  await api.close();
  const res = await proxyRequest(browser("GET", "collections"), ["collections"], { base, ...FAST });
  assert.equal(res.status, 502);
  const text = await res.text();
  assert.deepEqual(JSON.parse(text), { error: "api_unreachable" });
  assert.ok(!text.includes("127.0.0.1"));
});
