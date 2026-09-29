// Same-origin JSON pass-through to the FastAPI service, so the browser keeps
// the session cookie on the web origin and never talks to the API port
// directly. This proxy makes no auth or permission decision: the API checks the
// session, CSRF token, Origin and ACL on every request. Uploads and downloads
// use their own streaming routes (app/api/uploads, app/api/files).

// First path segment the proxy forwards; anything else is 404 here.
export const ALLOWED_PREFIXES = new Set([
  "auth", "admin", "library", "uploads", "discovery", "search", "collections", "rag",
]);
const REQUEST_HEADERS = ["cookie", "content-type", "accept", "origin", "x-csrf-token"];
const RESPONSE_HEADERS = ["content-type", "cache-control"];

// Routes whose API handler waits on OpenRouter synchronously. RAG runs query
// embedding (hybrid) then generation; hybrid search runs query embedding then
// rerank. Their API-side provider timeouts (RAS_EMBEDDING/RERANK/GENERATION
// __REQUEST_TIMEOUT_SECONDS, default 30/20/60 s) must fit inside this budget
// so the caller sees the API's own error rather than a proxy 504.
const PROVIDER_ROUTES = new Set(["POST /search", "POST /rag/ask"]);

export const DEFAULT_TIMEOUT_SECONDS = 15;
export const DEFAULT_PROVIDER_TIMEOUT_SECONDS = 120;
// undici (Node fetch) aborts on its own after 300 s without headers or body.
export const MAX_TIMEOUT_SECONDS = 300;

export type ProxyConfig = { base: string; timeoutMs: number; providerTimeoutMs: number };

function seconds(raw: string | undefined, fallback: number): number | null {
  if (raw === undefined || raw.trim() === "") return fallback;
  if (!/^\d+$/.test(raw.trim())) return null;
  const value = Number(raw.trim());
  return value >= 1 && value <= MAX_TIMEOUT_SECONDS ? value : null;
}

// Invalid values make the proxy refuse requests instead of guessing a limit.
export function loadProxyConfig(env: Record<string, string | undefined>): ProxyConfig | null {
  const base = env.API_INTERNAL_URL;
  const timeout = seconds(env.WEB_PROXY_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS);
  const provider = seconds(env.WEB_PROXY_PROVIDER_TIMEOUT_SECONDS, DEFAULT_PROVIDER_TIMEOUT_SECONDS);
  if (!base || timeout === null || provider === null || provider < timeout) return null;
  return { base: base.replace(/\/+$/, ""), timeoutMs: timeout * 1000, providerTimeoutMs: provider * 1000 };
}

export function timeoutFor(method: string, path: string[], config: ProxyConfig): number {
  return PROVIDER_ROUTES.has(`${method} /${path.join("/")}`) ? config.providerTimeoutMs : config.timeoutMs;
}

// Fixed bodies: never echo upstream errors, URLs or payloads to the browser.
function failure(status: number, error: string): Response {
  return Response.json({ error }, { status, headers: { "cache-control": "no-store" } });
}

function allowed(path: string[]): boolean {
  // Dot segments would be collapsed by URL parsing and escape the allowlist.
  return path.length > 0 && ALLOWED_PREFIXES.has(path[0])
    && path.every((s) => s !== "" && s !== "." && s !== ".." && !/[/\\]/.test(s));
}

export async function proxyRequest(request: Request, path: string[], config: ProxyConfig | null): Promise<Response> {
  if (!config) return failure(503, "api_not_configured");
  if (!allowed(path)) return failure(404, "not_found");
  const target = new URL(`${config.base}/${path.map(encodeURIComponent).join("/")}`);
  target.search = new URL(request.url).search;

  const headers = new Headers();
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  let body: ArrayBuffer | undefined;
  if (request.method !== "GET" && request.method !== "HEAD") {
    try {
      body = await request.arrayBuffer();
    } catch {
      return failure(400, "request_body_unreadable");
    }
  }

  // One deadline covers waiting for headers and reading the whole body.
  const signal = AbortSignal.timeout(timeoutFor(request.method, path, config));
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method, headers, body, redirect: "manual", cache: "no-store", signal,
    });
  } catch {
    return signal.aborted ? failure(504, "api_timeout") : failure(502, "api_unreachable");
  }
  let payload: ArrayBuffer | null = null;
  try {
    if (upstream.status !== 204) payload = await upstream.arrayBuffer();
  } catch {
    return signal.aborted ? failure(504, "api_timeout") : failure(502, "api_response_incomplete");
  }

  const out = new Headers();
  for (const name of RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  for (const cookie of upstream.headers.getSetCookie()) out.append("set-cookie", cookie);
  return new Response(payload, { status: upstream.status, headers: out });
}
