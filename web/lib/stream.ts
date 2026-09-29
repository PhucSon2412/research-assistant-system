// Same-origin streaming GET for original files. Bytes flow from FastAPI to the
// browser as a ReadableStream, never through the buffering JSON proxy, and
// only the session cookie is forwarded: FastAPI decides access on every
// request and no object-store URL is ever issued (M1c, M6a review).

const RESPONSE_HEADERS = ["content-type", "content-length", "content-disposition"];

export async function streamFromApi(request: Request, segments: string[], base: string | undefined): Promise<Response> {
  if (!base) return Response.json({ error: "api_not_configured" }, { status: 503 });
  const headers = new Headers();
  const cookie = request.headers.get("cookie");
  if (cookie) headers.set("cookie", cookie);
  try {
    const upstream = await fetch(`${base.replace(/\/+$/, "")}/${segments.map(encodeURIComponent).join("/")}`, {
      headers, cache: "no-store", redirect: "manual",
    });
    const out = new Headers({ "cache-control": "no-store", "x-content-type-options": "nosniff" });
    for (const name of RESPONSE_HEADERS) {
      const value = upstream.headers.get(name);
      if (value) out.set(name, value);
    }
    return new Response(upstream.body, { status: upstream.status, headers: out });
  } catch {
    return Response.json({ error: "api_unreachable" }, { status: 502 });
  }
}
