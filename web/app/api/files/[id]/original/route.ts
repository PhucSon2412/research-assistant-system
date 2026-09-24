// Every download goes back through FastAPI ACL. No signed object URL.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request, ctx: { params: Promise<{ id: string }> }): Promise<Response> {
  const base = process.env.API_INTERNAL_URL;
  if (!base) return Response.json({ error: "api_not_configured" }, { status: 503 });
  const { id } = await ctx.params;
  const headers = new Headers();
  const cookie = request.headers.get("cookie");
  if (cookie) headers.set("cookie", cookie);
  try {
    const upstream = await fetch(`${base}/files/${encodeURIComponent(id)}/original`, {
      headers, cache: "no-store", redirect: "manual",
    });
    const out = new Headers({ "cache-control": "no-store", "x-content-type-options": "nosniff" });
    for (const name of ["content-type", "content-length", "content-disposition"]) {
      const value = upstream.headers.get(name);
      if (value) out.set(name, value);
    }
    return new Response(upstream.body, { status: upstream.status, headers: out });
  } catch {
    return Response.json({ error: "api_unreachable" }, { status: 502 });
  }
}
