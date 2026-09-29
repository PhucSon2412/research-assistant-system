// Same-origin pass-through to the FastAPI service, so the browser keeps the
// session cookie on the web origin and never talks to the API port directly.
// This proxy makes no auth or permission decision: the API checks the
// session, CSRF token and ACL on every request.

export const dynamic = "force-dynamic";

const ALLOWED_PREFIXES = new Set(["auth", "admin", "library", "uploads", "discovery"]);
const REQUEST_HEADERS = ["cookie", "content-type", "accept", "origin", "x-csrf-token"];
const RESPONSE_HEADERS = ["content-type", "cache-control"];

async function proxy(request: Request, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const base = process.env.API_INTERNAL_URL;
  if (!base) return Response.json({ error: "api_not_configured" }, { status: 503 });
  const { path } = await ctx.params;
  if (!path.length || !ALLOWED_PREFIXES.has(path[0])) {
    return Response.json({ error: "not_found" }, { status: 404 });
  }
  const target = new URL(`${base}/${path.map(encodeURIComponent).join("/")}`);
  target.search = new URL(request.url).search;

  const headers = new Headers();
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: hasBody ? await request.arrayBuffer() : undefined,
      redirect: "manual",
      cache: "no-store",
      signal: AbortSignal.timeout(15000),
    });
  } catch {
    return Response.json({ error: "api_unreachable" }, { status: 502 });
  }

  const out = new Headers();
  for (const name of RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  for (const cookie of upstream.headers.getSetCookie()) out.append("set-cookie", cookie);
  const body = upstream.status === 204 ? null : await upstream.arrayBuffer();
  return new Response(body, { status: upstream.status, headers: out });
}

export { proxy as GET, proxy as POST, proxy as PATCH, proxy as DELETE };
