/**
 * survo.tech Worker entry point.
 *   /                 static site (assets binding, served before the Worker)
 *   /s/:rid           click tracking -> KoboToolbox redirect
 *   /optout/:rid      opt-out page + RFC 8058 one-click POST
 *   /bimi/logo.svg    BIMI logo (SVG Tiny PS) with an explicit image/svg+xml content type
 *   /dashboard[/api]  researcher dashboard (Cloudflare Access JWT or Basic auth)
 *   /api/*            job API (Bearer API_TOKEN)
 *   cron              housekeeping every 15 minutes
 */
import { handleApi } from "./api";
import { requireDashboardUser } from "./auth";
import { runCron } from "./cron";
import { handleDashboard } from "./dashboard";
import { handleClick, handleOptout } from "./public";
import { Env } from "./util";

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(req.url);
    // Canonical host: www -> apex.
    if (url.hostname === "www.survo.tech") {
      url.hostname = "survo.tech";
      return Response.redirect(url.toString(), 301);
    }
    const p = url.pathname;

    let m = /^\/s\/([^/]+)\/?$/.exec(p);
    if (m) return handleClick(req, env, ctx, m[1]);
    if (p === "/s" || p === "/s/") return Response.redirect(env.KOBO_FORM_URL, 302);

    m = /^\/optout\/([^/]*)\/?$/.exec(p);
    if (m) return handleOptout(req, env, m[1]);

    if (p === "/dashboard" || p.startsWith("/dashboard/")) {
      const who = await requireDashboardUser(req, env);
      if (who instanceof Response) return who;
      return handleDashboard(req, env, url, who);
    }

    if (p.startsWith("/api/")) return handleApi(req, env, url);

    if (p === "/bimi/logo.svg") return serveBimiLogo(req, env, url);

    // Anything else: static assets (404 page from assets if missing).
    const res = await env.ASSETS.fetch(req);
    if (res.status === 404) return new Response("Not found", { status: 404, headers: { "content-type": "text/plain" } });
    return res;
  },

  async scheduled(_event: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(runCron(env).then(() => undefined));
  },
};

/** BIMI logo: served from the static assets but always with Content-Type image/svg+xml. */
async function serveBimiLogo(req: Request, env: Env, url: URL): Promise<Response> {
  if (req.method !== "GET" && req.method !== "HEAD") return new Response("Method not allowed", { status: 405 });
  const res = await env.ASSETS.fetch(new Request(new URL("/bimi/logo.svg", url.origin).toString(), { method: "GET" }));
  if (!res.ok) return new Response("Not found", { status: 404, headers: { "content-type": "text/plain" } });
  return new Response(req.method === "HEAD" ? null : res.body, {
    status: 200,
    headers: {
      "content-type": "image/svg+xml",
      "cache-control": "public, max-age=86400",
      "x-content-type-options": "nosniff",
      "access-control-allow-origin": "*",
    },
  });
}
