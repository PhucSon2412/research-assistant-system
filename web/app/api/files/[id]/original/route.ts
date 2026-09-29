// Every download goes back through FastAPI ACL. No signed object URL.
import { streamFromApi } from "../../../../../lib/stream";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request, ctx: { params: Promise<{ id: string }> }): Promise<Response> {
  const { id } = await ctx.params;
  return streamFromApi(request, ["files", id, "original"], process.env.API_INTERNAL_URL);
}
