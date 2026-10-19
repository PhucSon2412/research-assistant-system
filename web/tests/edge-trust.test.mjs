import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { CLIENT_IP_HEADER, EDGE_TOKEN_HEADER, loadProxyConfig, proxyRequest, trustedClientIp } from "../lib/proxy.ts";
import { ApiError, describeError, invitationError, parseRetryAfter } from "../lib/api.ts";

// M8a trusted chain: synthetic token and addresses only.
const TOKEN = "synthetic-edge-token-0123456789abcdef";
const ORIGIN = "https://ras.test";

function tokenFile(value) {
  const dir = mkdtempSync(join(tmpdir(), "ras-edge-"));
  const path = join(dir, "edge-token");
  writeFileSync(path, value);
  return path;
}

function request(headers) {
  return new Request(`${ORIGIN}/api/auth/login`, {
    method: "POST", body: "{}",
    headers: { origin: ORIGIN, "content-type": "application/json", "x-csrf-token": "c", ...headers },
  });
}

async function upstream(status = 200, extra = {}) {
  const seen = [];
  const server = createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      seen.push(req.headers);
      res.writeHead(status, { "content-type": "application/json", ...extra });
      res.end(JSON.stringify({ error: status === 429 ? "rate_limited" : "ok" }));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  return { base: `http://127.0.0.1:${server.address().port}`, seen,
    close: () => { server.closeAllConnections(); return new Promise((r) => server.close(r)); } };
}

test("client address is forwarded only with the matching edge token", async () => {
  const api = await upstream();
  const config = loadProxyConfig({ API_INTERNAL_URL: api.base, WEB_EDGE_TOKEN_FILE: tokenFile(TOKEN + "\n") });
  assert.ok(config && config.edgeToken === TOKEN);
  try {
    const cases = [
      [{ [EDGE_TOKEN_HEADER]: TOKEN, [CLIENT_IP_HEADER]: "203.0.113.9" }, "203.0.113.9"],
      [{ [EDGE_TOKEN_HEADER]: TOKEN, [CLIENT_IP_HEADER]: "2001:db8::7" }, "2001:db8::7"],
      [{ [EDGE_TOKEN_HEADER]: TOKEN.slice(0, -1) + "x", [CLIENT_IP_HEADER]: "203.0.113.9" }, undefined],
      [{ [EDGE_TOKEN_HEADER]: TOKEN + "x", [CLIENT_IP_HEADER]: "203.0.113.9" }, undefined],
      [{ [CLIENT_IP_HEADER]: "203.0.113.9" }, undefined],
      [{ [EDGE_TOKEN_HEADER]: TOKEN, [CLIENT_IP_HEADER]: "203.0.113.9, 198.51.100.1" }, undefined],
      [{ [EDGE_TOKEN_HEADER]: TOKEN, [CLIENT_IP_HEADER]: "not-an-ip" }, undefined],
      [{ [EDGE_TOKEN_HEADER]: TOKEN }, undefined],
    ];
    for (const [headers, expected] of cases) {
      await proxyRequest(request({ ...headers, "x-forwarded-for": "198.51.100.66", "x-real-ip": "198.51.100.67",
        forwarded: "for=198.51.100.68" }), ["auth", "login"], config);
      const seen = api.seen.at(-1);
      assert.equal(seen[CLIENT_IP_HEADER], expected);
      assert.equal(seen[EDGE_TOKEN_HEADER], undefined);
      for (const name of ["x-forwarded-for", "x-real-ip", "forwarded"]) assert.equal(seen[name], undefined);
    }
  } finally { await api.close(); }
});

test("without a configured token nothing is trusted; a bad token file refuses requests", async () => {
  const api = await upstream();
  try {
    const open = loadProxyConfig({ API_INTERNAL_URL: api.base });
    assert.equal(open.edgeToken, null);
    assert.equal(trustedClientIp(request({ [EDGE_TOKEN_HEADER]: TOKEN, [CLIENT_IP_HEADER]: "203.0.113.9" }), open), null);
    for (const env of [{ WEB_EDGE_TOKEN_FILE: tokenFile("short") }, { WEB_EDGE_TOKEN_FILE: join(tmpdir(), "missing-ras-token") },
      { WEB_EDGE_TOKEN_FILE: tokenFile("has space " + TOKEN) }]) {
      const config = loadProxyConfig({ API_INTERNAL_URL: api.base, ...env });
      assert.equal(config, null);
      const response = await proxyRequest(request({}), ["auth", "login"], config);
      assert.equal(response.status, 503);
      assert.deepEqual(await response.json(), { error: "api_not_configured" });
    }
    assert.equal(api.seen.length, 0);
  } finally { await api.close(); }
});

test("429 and Retry-After pass through unchanged", async () => {
  const api = await upstream(429, { "retry-after": "17", "x-internal": "drop" });
  try {
    const response = await proxyRequest(request({}), ["auth", "login"], loadProxyConfig({ API_INTERNAL_URL: api.base }));
    assert.equal(response.status, 429);
    assert.equal(response.headers.get("retry-after"), "17");
    assert.equal(response.headers.get("x-internal"), null);
  } finally { await api.close(); }
});

test("UI shows the wait without resubmitting and invitation timeouts stay unknown", () => {
  assert.equal(parseRetryAfter("17"), 17);
  for (const raw of [null, "", "0", "-1", "1.5", "Wed, 21 Oct 2026 07:28:00 GMT", "9999999"]) {
    assert.equal(parseRetryAfter(raw), undefined);
  }
  assert.match(describeError(new ApiError(429, "rate_limited", undefined, 17)), /khoảng 17 giây/);
  assert.match(describeError(new ApiError(429, "rate_limited")), /Hãy chờ/);
  assert.match(describeError(new ApiError(503, "rate_limit_unavailable")), /chưa được thực hiện/);
  assert.match(describeError(new ApiError(503, "delivery_failed")), /đã bị hủy/);
  assert.match(invitationError(new ApiError(504, "api_timeout")), /Không xác định được kết quả gửi lời mời/);
  assert.match(invitationError(new ApiError(503, "delivery_failed")), /đã bị hủy/);
});
