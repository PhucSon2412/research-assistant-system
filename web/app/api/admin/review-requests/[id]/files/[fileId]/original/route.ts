// Admin review read of the one original named by an open request (M6a).
// FastAPI re-checks the Admin session and the open request on every GET, so a
// withdrawn/rejected request stops new downloads even from an old link.
import { streamFromApi } from "../../../../../../../../lib/stream";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request, ctx: { params: Promise<{ id: string; fileId: string }> }): Promise<Response> {
  const { id, fileId } = await ctx.params;
  return streamFromApi(request, ["admin", "review-requests", id, "files", fileId, "original"],
    process.env.API_INTERNAL_URL);
}
