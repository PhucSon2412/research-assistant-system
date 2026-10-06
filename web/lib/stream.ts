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
  const controller = new AbortController();
  const abort = () => controller.abort();
  request.signal.addEventListener("abort", abort, { once: true });
  if (request.signal.aborted) abort();
  const detach = () => request.signal.removeEventListener("abort", abort);
  try {
    const upstream = await fetch(`${base.replace(/\/+$/, "")}/${segments.map(encodeURIComponent).join("/")}`, {
      headers, cache: "no-store", redirect: "manual", signal: controller.signal,
    });
    const out = new Headers({ "cache-control": "no-store", "x-content-type-options": "nosniff" });
    for (const name of RESPONSE_HEADERS) {
      const value = upstream.headers.get(name);
      if (value) out.set(name, value);
    }
    for (const cookie of upstream.headers.getSetCookie()) out.append("set-cookie", cookie);
    if (!upstream.body) {
      detach();
      return new Response(null, { status: upstream.status, headers: out });
    }
    const reader = upstream.body.getReader();
    const body = new ReadableStream<Uint8Array>({
      async pull(stream) {
        try {
          const { done, value } = await reader.read();
          if (done) { detach(); stream.close(); }
          else stream.enqueue(value);
        } catch (error) {
          detach(); controller.abort(); stream.error(error);
        }
      },
      async cancel(reason) {
        detach(); controller.abort();
        try { await reader.cancel(reason); } catch { /* already aborted */ }
      },
    }, { highWaterMark: 0 });
    return new Response(body, { status: upstream.status, headers: out });
  } catch {
    detach(); controller.abort();
    return Response.json({ error: "api_unreachable" }, { status: 502 });
  }
}
