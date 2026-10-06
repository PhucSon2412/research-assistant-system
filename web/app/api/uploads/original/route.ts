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
    const out = new Headers({ "content-type": upstream.headers.get("content-type") ?? "application/json", "cache-control": "no-store" });
    for (const cookie of upstream.headers.getSetCookie()) out.append("set-cookie", cookie);
    return new Response(upstream.body, {
      status: upstream.status,
      headers: out,
    });
  } catch {
    // A proxy timeout / disconnect cannot establish the backend PUT outcome.
    return Response.json({ error: "upload_outcome_uncertain" }, { status: 502 });
  }
}
