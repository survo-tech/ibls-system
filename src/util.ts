/** Small shared helpers for the Worker. */
import { isoOf } from "./schedule";

export interface Env {
  DB: D1Database;
  ASSETS: Fetcher;
  SITE_ORIGIN: string;
  KOBO_FORM_URL: string;
  ACCESS_TEAM_DOMAIN?: string;
  ACCESS_AUD?: string;
  API_TOKEN?: string;
  DASHBOARD_USERS?: string;
  MAILBOXES?: string;
  ALLOW_TIME_OVERRIDE?: string; // local tests only
}

/** The five Google Workspace sending mailboxes (override with the MAILBOXES var, comma separated). */
export const DEFAULT_MAILBOXES = ["participate@survo.tech", "research@survo.tech", "survey@survo.tech", "study@survo.tech", "team@survo.tech"];

export function mailboxes(env: Env): string[] {
  const v = (env.MAILBOXES || "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
  return v.length ? v : DEFAULT_MAILBOXES;
}

/** Canonical timestamp format used everywhere in D1. */
export function nowIso(): string {
  return isoOf(new Date());
}

/** Normalise any parseable timestamp to canonical ISO UTC; falls back to now. */
export function normTs(v: unknown): string {
  if (typeof v === "string" || typeof v === "number") {
    const d = new Date(v);
    if (!Number.isNaN(d.getTime())) return isoOf(d);
  }
  return nowIso();
}

export const RID_RE = /^[A-Z0-9]{4,16}$/;

export function normRid(v: unknown): string | null {
  if (typeof v !== "string") return null;
  const r = v.trim().toUpperCase();
  return RID_RE.test(r) ? r : null;
}

export function json(data: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers },
  });
}

export function htmlResponse(body: string, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(body, {
    status,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
      ...headers,
    },
  });
}

export function escapeHtml(s: unknown): string {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

/** Constant-time string comparison (length leak only). */
export function safeEqual(a: string, b: string): boolean {
  const ea = new TextEncoder().encode(a);
  const eb = new TextEncoder().encode(b);
  let diff = ea.length ^ eb.length;
  const n = Math.max(ea.length, eb.length);
  for (let i = 0; i < n; i++) diff |= (ea[i] ?? 0) ^ (eb[i] ?? 0);
  return diff === 0;
}

export async function sha256Hex(s: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function randomId(prefix = ""): string {
  const b = new Uint8Array(9);
  crypto.getRandomValues(b);
  return prefix + [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
}

/** Split an array into chunks (D1 allows max 100 bound parameters per statement). */
export function chunks<T>(arr: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < arr.length; i += size) out.push(arr.slice(i, i + size));
  return out;
}

export function placeholders(n: number): string {
  return Array(n).fill("?").join(",");
}

/** Very rough check for security scanners / link-preview bots that pre-fetch links in emails. */
export function looksLikeBot(ua: string): boolean {
  if (!ua) return true;
  return /bot|crawl|spider|preview|scan|proofpoint|mimecast|barracuda|safelinks|urldefense|python|curl|wget|headless|go-http|java\/|okhttp|libwww|httpclient|phantom|facebookexternalhit|slurp/i.test(ua);
}

export function csvCell(v: unknown): string {
  const s = v === null || v === undefined ? "" : String(v);
  // Neutralise spreadsheet formula injection, then quote.
  const safe = /^[=+\-@\t\r]/.test(s) ? "'" + s : s;
  return /[",\n\r]/.test(safe) ? '"' + safe.replace(/"/g, '""') + '"' : safe;
}
