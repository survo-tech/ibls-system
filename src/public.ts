/** Public routes: /s/:rid click-tracking redirect and /optout/:rid. */
import { ingestEvents } from "./db";
import { Env, escapeHtml, htmlResponse, looksLikeBot, normRid } from "./util";

/** GET /s/:rid -> record the click, 302 to the Kobo form with the rid prefilled. */
export async function handleClick(req: Request, env: Env, ctx: ExecutionContext, raw: string): Promise<Response> {
  const rid = normRid(decodeURIComponent(raw || ""));
  const base = env.KOBO_FORM_URL;
  let target = base;
  if (rid) {
    const known = await env.DB.prepare("SELECT rid FROM contacts WHERE rid = ?").bind(rid).first<{ rid: string }>();
    if (known) target = `${base}?d[rid]=${encodeURIComponent(rid)}`;
    if (req.method === "GET") {
      const ua = req.headers.get("user-agent") || "";
      const cf = (req as Request & { cf?: { country?: string } }).cf;
      const meta = { ua: ua.slice(0, 300), country: cf?.country || null, known: !!known };
      // Record asynchronously so the redirect is instant.
      ctx.waitUntil(ingestEvents(env.DB, [{ rid, type: "click", kind: looksLikeBot(ua) ? "bot" : "human", meta }]).catch(() => undefined));
    }
  }
  return new Response(null, { status: 302, headers: { location: target, "cache-control": "no-store", "referrer-policy": "no-referrer" } });
}

/** GET shows a confirm page; POST (form or RFC 8058 one-click) records the opt-out. */
export async function handleOptout(req: Request, env: Env, raw: string): Promise<Response> {
  const rid = normRid(decodeURIComponent(raw || ""));
  if (req.method === "GET" || req.method === "HEAD") {
    return htmlResponse(page("Stop emails about this study", `
      <p class="head__sub">Click the button below and we will not email you about the India Business Leaders Survey again.</p>
      <form method="post" action="/optout/${escapeHtml(rid || "")}">
        <input type="hidden" name="confirm" value="1" />
        <div class="links"><button class="btn" type="submit">Confirm opt-out</button><a class="btn" href="/">Back to the study page</a></div>
      </form>
      <p class="fineprint">You can also reply “OPT OUT” to any of our emails or write to <a class="link" href="mailto:participate@survo.tech">participate@survo.tech</a>.</p>`), 200, { "x-robots-tag": "noindex" });
  }
  if (req.method !== "POST") return new Response("Method not allowed", { status: 405 });

  let body = "";
  try { body = await req.text(); } catch { /* empty body */ }
  const params = new URLSearchParams(body);
  const oneClick = params.get("List-Unsubscribe") === "One-Click";
  if (rid) {
    await ingestEvents(env.DB, [{
      rid, type: "opted_out",
      meta: { source: oneClick ? "one_click" : "web", ua: (req.headers.get("user-agent") || "").slice(0, 200) },
      id: `optout:${rid}`, // one opt-out event per contact
    }]);
  }
  if (oneClick) return new Response("Unsubscribed", { status: 200, headers: { "content-type": "text/plain", "cache-control": "no-store" } });
  // Same confirmation whether or not the rid exists (no enumeration).
  return htmlResponse(page("You have been opted out", `
    <p class="head__sub">Thank you. We will not email you about this study again.</p>
    <p>If you change your mind, the study page explains how to take part.</p>
    <div class="links"><a class="btn" href="/">Back to the study page</a></div>`), 200, { "x-robots-tag": "noindex" });
}

/** Minimal page that reuses the site's stylesheet. */
export function page(title: string, inner: string): string {
  return `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="robots" content="noindex" />
<title>${escapeHtml(title)} · India Business Leaders Survey</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&display=swap" />
<link rel="stylesheet" href="/site.css" />
<link rel="icon" href="/favicon.svg" type="image/svg+xml" />
<style>.x-wrap{max-width:720px;margin:0 auto;padding:12vh 24px 48px} button.btn{cursor:pointer;font:inherit}</style>
</head><body><div class="page"><main class="main"><section class="section x-wrap">
<div class="head"><p class="head__eyebrow">India Business Leaders Survey</p><h2>${escapeHtml(title)}</h2></div>
${inner}
</section></main>
<footer class="foot"><p>India Business Leaders Survey · London Business School · MIT · Harvard University · Fieldwork: Morsel Research and Development Pvt Ltd · REC1101</p>
<p>Fieldwork contact: Bishnu Datta Behuria, Morsel Research and Development · <a class="link" href="mailto:bishnu@morselindia.org">bishnu@morselindia.org</a></p></footer>
</div></body></html>`;
}
