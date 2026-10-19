/**
 * Authentication.
 *  - /api/*       : Bearer API_TOKEN (GitHub Actions jobs).
 *  - /dashboard*  : Cloudflare Access JWT (Cf-Access-Jwt-Assertion) verified against the team's JWKS,
 *                   OR HTTP Basic against DASHBOARD_USERS ("user:sha256hex,..."). Fails closed if neither
 *                   mechanism is configured.
 */
import { Env, safeEqual, sha256Hex } from "./util";

export function checkBearer(req: Request, env: Env): boolean {
  const token = env.API_TOKEN || "";
  if (token.length < 16) return false; // refuse to run with a missing/weak token
  const h = req.headers.get("authorization") || "";
  const m = /^Bearer\s+(.+)$/i.exec(h);
  return !!m && safeEqual(m[1].trim(), token);
}

let jwksCache: { at: number; team: string; keys: JsonWebKey[] } | null = null;

async function accessKeys(team: string): Promise<JsonWebKey[]> {
  if (jwksCache && jwksCache.team === team && Date.now() - jwksCache.at < 3600_000) return jwksCache.keys;
  const res = await fetch(`https://${team}/cdn-cgi/access/certs`, { cf: { cacheTtl: 3600 } } as RequestInit);
  if (!res.ok) throw new Error(`Access certs HTTP ${res.status}`);
  const body = (await res.json()) as { keys: JsonWebKey[] };
  jwksCache = { at: Date.now(), team, keys: body.keys || [] };
  return jwksCache.keys;
}

function b64urlDecode(s: string): Uint8Array {
  const pad = s.length % 4 ? "=".repeat(4 - (s.length % 4)) : "";
  const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + pad);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Verify a Cloudflare Access JWT. Returns the user's email (or sub) when valid, else null. */
export async function verifyAccessJwt(token: string, env: Env): Promise<string | null> {
  const team = (env.ACCESS_TEAM_DOMAIN || "").replace(/^https?:\/\//, "").replace(/\/$/, "");
  const aud = env.ACCESS_AUD || "";
  if (!team || !aud || !token) return null;
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const header = JSON.parse(new TextDecoder().decode(b64urlDecode(parts[0])));
    const payload = JSON.parse(new TextDecoder().decode(b64urlDecode(parts[1])));
    if (header.alg !== "RS256") return null;
    const jwk = (await accessKeys(team)).find((k) => (k as { kid?: string }).kid === header.kid);
    if (!jwk) return null;
    const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64urlDecode(parts[2]), new TextEncoder().encode(parts[0] + "." + parts[1]));
    if (!ok) return null;
    const now = Math.floor(Date.now() / 1000);
    if (typeof payload.exp === "number" && payload.exp < now - 30) return null;
    if (typeof payload.nbf === "number" && payload.nbf > now + 30) return null;
    const auds: string[] = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
    if (!auds.includes(aud)) return null;
    if (payload.iss && payload.iss !== `https://${team}`) return null;
    return String(payload.email || payload.sub || "access-user");
  } catch {
    return null;
  }
}

/** HTTP Basic against DASHBOARD_USERS. Returns username when valid. */
export async function verifyBasic(req: Request, env: Env): Promise<string | null> {
  const users = (env.DASHBOARD_USERS || "").split(",").map((s) => s.trim()).filter(Boolean);
  if (!users.length) return null;
  const h = req.headers.get("authorization") || "";
  const m = /^Basic\s+(.+)$/i.exec(h);
  if (!m) return null;
  let decoded = "";
  try { decoded = atob(m[1].trim()); } catch { return null; }
  const i = decoded.indexOf(":");
  if (i < 1) return null;
  const user = decoded.slice(0, i), pass = decoded.slice(i + 1);
  const hash = await sha256Hex(pass);
  let match: string | null = null;
  for (const entry of users) {
    const j = entry.indexOf(":");
    if (j < 1) continue;
    const u = entry.slice(0, j), h2 = entry.slice(j + 1).toLowerCase();
    // compare every entry to keep timing flat
    if (safeEqual(u, user) && safeEqual(h2, hash)) match = u;
  }
  return match;
}

/** Returns the actor (email/username) or a 401/403 Response. */
export async function requireDashboardUser(req: Request, env: Env): Promise<string | Response> {
  const jwt = req.headers.get("cf-access-jwt-assertion");
  if (jwt) {
    const who = await verifyAccessJwt(jwt, env);
    if (who) return who;
  }
  const basic = await verifyBasic(req, env);
  if (basic) return basic;
  const configured = !!((env.ACCESS_TEAM_DOMAIN && env.ACCESS_AUD) || env.DASHBOARD_USERS);
  if (!configured) return new Response("Dashboard auth is not configured (set DASHBOARD_USERS or Cloudflare Access).", { status: 503 });
  return new Response("Authentication required", {
    status: 401,
    headers: env.DASHBOARD_USERS ? { "www-authenticate": 'Basic realm="IBLS dashboard", charset="UTF-8"' } : {},
  });
}
