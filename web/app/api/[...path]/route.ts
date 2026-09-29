// Same-origin JSON proxy to FastAPI; allowlist, timeouts and error handling
// live in lib/proxy.ts. The API, not this route, decides every permission.
import { loadProxyConfig, proxyRequest } from "../../../lib/proxy";

export const dynamic = "force-dynamic";

async function proxy(request: Request, ctx: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await ctx.params;
  return proxyRequest(request, path, loadProxyConfig(process.env));
}

export { proxy as GET, proxy as POST, proxy as PATCH, proxy as DELETE };
