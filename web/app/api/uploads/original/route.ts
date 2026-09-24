// Dedicated same-origin streaming bridge. FastAPI owns session, CSRF, intent
// and file validation; this route never reads the whole PDF into memory.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function PUT(request: Request): Promise<Response> {
  const base = process.env.API_INTERNAL_URL;
  if (!base) return Response.json({ error: "api_not_configured" }, { status: 503 });
  const headers = new Headers();
  for (const name of ["cookie", "origin", "x-csrf-token", "x-upload-intent", "content-type"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  try {
    const upstream = await fetch(`${base}/uploads/original`, {
      method: "PUT",
      headers,
      body: request.body,
      duplex: "half",
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(120_000),
    } as RequestInit & { duplex: "half" });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "content-type": upstream.headers.get("content-type") ?? "application/json", "cache-control": "no-store" },
    });
  } catch {
    return Response.json({ error: "api_unreachable" }, { status: 502 });
  }
}
