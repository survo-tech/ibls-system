/** Settings access and event ingestion (the single place where contact state changes). */
import { DAILY_HARD_MAX, isoOf } from "./schedule";
import { chunks, normRid, normTs, nowIso, placeholders } from "./util";

export const DEFAULT_SETTINGS: Record<string, string> = {
  paused: "false",
  pause_reason: "",
  test_mode: "true",
  test_recipients: "",
  start_date: "2026-10-27",
  end_date: "",
  target_end_date: "2026-12-18",
  window_start: "10:00",
  window_end: "17:00",
  send_weekdays: "1,2,3,4,5",
  blackout_dates: "2026-11-05..2026-11-11",
  warmup_caps: "20,40,60,100,150,200,250",
  warmup_seed_cap: "5",
  warmup_max_bounce_rate: "0.02",
  warmup_stepback_bounce_rate: "0.04",
  warmup_window_days: "3",
  cap_max: "450",
  hourly_cap: "120",
  per_run_max: "20",
  pace_evenly: "true",
  reminder_after_workdays: "5",
  reminder2_enabled: "false",
  reminder2_after_workdays: "5",
  postal_address: "",
  from_name: "India Business Leaders Survey",
  reply_to: "research@survo.tech",
  complaint_threshold: "3",
  bounce_rate_threshold: "0.03",
  bounce_window: "200",
  bounce_min_sample: "50",
  heartbeat_max_minutes: "90",
  bounce_guard_mailbox_hour: "4",
  bounce_guard_mailbox_day: "8",
  bounce_guard_mailbox_3day: "18",
  bounce_guard_domain_day: "30",
  bounce_guard_domain_3day: "75",
  stop_rule_reset_at: "",
  stop_rule_complaint_baseline: "0",
};

/** Keys a researcher may edit from the dashboard, with validators. */
export const EDITABLE: Record<string, (v: string) => boolean> = {
  test_mode: (v) => v === "true" || v === "false",
  test_recipients: (v) => v.split(/[\s,;]+/).filter(Boolean).every((e) => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e)),
  start_date: isDate,
  end_date: (v) => v === "" || isDate(v),
  target_end_date: (v) => v === "" || isDate(v),
  window_start: isHHMM,
  window_end: isHHMM,
  send_weekdays: (v) => /^[1-7](\s*,\s*[1-7])*$/.test(v),
  blackout_dates: (v) => v.split(/[\s,;]+/).filter(Boolean).every((p) => /^\d{4}-\d{2}-\d{2}(\.\.\d{4}-\d{2}-\d{2})?$/.test(p)),
  warmup_caps: (v) => /^\d+(\s*,\s*\d+)*$/.test(v) && v.split(",").every((n) => +n >= 0 && +n <= DAILY_HARD_MAX),
  warmup_seed_cap: (v) => isInt(v, 0, 50),
  warmup_max_bounce_rate: (v) => Number(v) > 0 && Number(v) < 1,
  warmup_stepback_bounce_rate: (v) => Number(v) > 0 && Number(v) < 1,
  warmup_window_days: (v) => isInt(v, 1, 30),
  cap_max: (v) => isInt(v, 0, DAILY_HARD_MAX),
  hourly_cap: (v) => isInt(v, 0, DAILY_HARD_MAX),
  per_run_max: (v) => isInt(v, 0, 200),
  pace_evenly: (v) => v === "true" || v === "false",
  reminder_after_workdays: (v) => isInt(v, 1, 60),
  reminder2_enabled: (v) => v === "true" || v === "false",
  reminder2_after_workdays: (v) => isInt(v, 1, 60),
  postal_address: (v) => v.length <= 300,
  from_name: (v) => v.length > 0 && v.length <= 80 && !/[<>"@\r\n]/.test(v),
  // Replies must land in a mailbox the sync job reads, so Reply-To is restricted to survo.tech.
  reply_to: (v) => /^[a-z0-9._%+-]+@survo\.tech$/i.test(v),
  complaint_threshold: (v) => isInt(v, 1, 100),
  bounce_rate_threshold: (v) => Number(v) > 0 && Number(v) < 1,
  bounce_window: (v) => isInt(v, 20, 5000),
  bounce_min_sample: (v) => isInt(v, 1, 5000),
  heartbeat_max_minutes: (v) => isInt(v, 15, 1440),
  bounce_guard_mailbox_hour: (v) => isInt(v, 0, 1000),
  bounce_guard_mailbox_day: (v) => isInt(v, 0, 1000),
  bounce_guard_mailbox_3day: (v) => isInt(v, 0, 1000),
  bounce_guard_domain_day: (v) => isInt(v, 0, 1000),
  bounce_guard_domain_3day: (v) => isInt(v, 0, 1000),
};

function isDate(v: string) { return /^\d{4}-\d{2}-\d{2}$/.test(v) && !Number.isNaN(Date.parse(v)); }
function isHHMM(v: string) { return /^([01]\d|2[0-3]):[0-5]\d$/.test(v); }
function isInt(v: string, lo: number, hi: number) { return /^\d+$/.test(v) && +v >= lo && +v <= hi; }

export async function getSettings(db: D1Database): Promise<Record<string, string>> {
  const { results } = await db.prepare("SELECT key, value FROM settings WHERE key NOT LIKE 'state:%'").all<{ key: string; value: string }>();
  const out = { ...DEFAULT_SETTINGS };
  for (const r of results) out[r.key] = r.value;
  return out;
}

export async function setSettings(db: D1Database, kv: Record<string, string>): Promise<void> {
  const stmts = Object.entries(kv).map(([k, v]) =>
    db.prepare("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value").bind(k, v));
  if (stmts.length) await db.batch(stmts);
}

export async function getState(db: D1Database, keys: string[]): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  if (!keys.length) return out;
  const { results } = await db.prepare(`SELECT key, value FROM settings WHERE key IN (${placeholders(keys.length)})`)
    .bind(...keys.map((k) => "state:" + k)).all<{ key: string; value: string }>();
  for (const r of results) out[r.key.slice(6)] = r.value;
  return out;
}

export async function setState(db: D1Database, kv: Record<string, string>): Promise<void> {
  const mapped: Record<string, string> = {};
  for (const [k, v] of Object.entries(kv)) mapped["state:" + k] = v;
  await setSettings(db, mapped);
}

/**
 * Bounce guard: count bounces (DSNs the mailbox received + send-time rejects) per mailbox and domain-wide
 * over rolling windows and say whether the mailbox must stop for now. It is a short-term brake on bounce
 * spikes (protecting the domain's reputation with Gmail); the warm-up and the stop rules handle the longer
 * term. A limit of 0 disables that check.
 */
export async function bounceGuard(db: D1Database, s: Record<string, string>, mailbox: string, now: Date): Promise<{ blocked: boolean; reason?: string; counts: Record<string, number> }> {
  const ago = (h: number) => isoOf(new Date(now.getTime() - h * 3600_000));
  const c = await db.prepare(`SELECT
      COALESCE(SUM(mailbox = ?1 AND ts >= ?2), 0) AS mb_hour,
      COALESCE(SUM(mailbox = ?1 AND ts >= ?3), 0) AS mb_day,
      COALESCE(SUM(mailbox = ?1), 0) AS mb_3day,
      COALESCE(SUM(ts >= ?3), 0) AS dom_day,
      COUNT(*) AS dom_3day
    FROM events WHERE type IN ('bounced_hard', 'bounced_soft') AND ts >= ?4`).bind(mailbox, ago(1), ago(24), ago(72)).first<Record<string, number>>();
  const counts = { mb_hour: c?.mb_hour || 0, mb_day: c?.mb_day || 0, mb_3day: c?.mb_3day || 0, dom_day: c?.dom_day || 0, dom_3day: c?.dom_3day || 0 };
  const checks: [number, string, string][] = [
    [counts.mb_hour, s.bounce_guard_mailbox_hour, `${mailbox}: ${counts.mb_hour} bounces in the last hour`],
    [counts.mb_day, s.bounce_guard_mailbox_day, `${mailbox}: ${counts.mb_day} bounces in 24 h`],
    [counts.mb_3day, s.bounce_guard_mailbox_3day, `${mailbox}: ${counts.mb_3day} bounces in 72 h`],
    [counts.dom_day, s.bounce_guard_domain_day, `domain: ${counts.dom_day} bounces in 24 h`],
    [counts.dom_3day, s.bounce_guard_domain_3day, `domain: ${counts.dom_3day} bounces in 72 h`],
  ];
  for (const [n, lim, msg] of checks) {
    const L = Number(lim);
    if (L > 0 && n >= L) return { blocked: true, reason: `Bounce guard: ${msg} (limit ${L}); sending from this mailbox resumes when the window rolls over`, counts };
  }
  return { blocked: false, counts };
}

export function testRecipients(s: Record<string, string>): string[] {
  return s.test_recipients.split(/[\s,;]+/).map((e) => e.trim().toLowerCase()).filter(Boolean);
}

export const EVENT_TYPES = new Set([
  "sent", "failed", "bounced_hard", "bounced_soft", "complaint", "reply", "auto_reply", "opted_out",
  "started", "completed", "consent_declined", "click", "admin", "released",
]);

export interface IncomingEvent {
  rid?: string | null;
  email?: string | null;
  type: string;
  ts?: string;
  kind?: string | null;
  mailbox?: string | null;
  meta?: Record<string, unknown> | null;
  id?: string | null; // dedupe key supplied by the producer
}

interface Prepared {
  rid: string | null; email: string | null; type: string; ts: string; kind: string | null;
  mailbox: string | null; meta: Record<string, unknown>; dedupe: string | null; test: number;
}

/**
 * Ingest events idempotently and apply their effect on contacts/replies.
 * Returns counts. Unknown rids are stored with rid NULL (meta.rid_raw keeps the value).
 */
export async function ingestEvents(db: D1Database, events: IncomingEvent[]): Promise<{ accepted: number; duplicate: number; rejected: number; errors: string[] }> {
  const errors: string[] = [];
  let rejected = 0;
  const valid = events.filter((e) => {
    if (!e || typeof e !== "object" || !EVENT_TYPES.has(String(e.type))) { rejected++; errors.push(`bad type: ${e && (e as IncomingEvent).type}`); return false; }
    return true;
  });

  // Resolve rids / emails against contacts.
  const rids = [...new Set(valid.map((e) => normRid(e.rid)).filter(Boolean) as string[])];
  const emails = [...new Set(valid.filter((e) => !normRid(e.rid) && e.email).map((e) => String(e.email).trim().toLowerCase()))];
  const byRid = new Map<string, { rid: string; email: string; is_test: number }>();
  const byEmail = new Map<string, { rid: string; email: string; is_test: number }>();
  for (const c of chunks(rids, 90)) {
    const { results } = await db.prepare(`SELECT rid, email, is_test FROM contacts WHERE rid IN (${placeholders(c.length)})`).bind(...c).all<{ rid: string; email: string; is_test: number }>();
    for (const r of results) byRid.set(r.rid, r);
  }
  for (const c of chunks(emails, 90)) {
    const { results } = await db.prepare(`SELECT rid, email, is_test FROM contacts WHERE email IN (${placeholders(c.length)})`).bind(...c).all<{ rid: string; email: string; is_test: number }>();
    for (const r of results) byEmail.set(r.email, r);
  }

  const prepared: Prepared[] = valid.map((e) => {
    const meta = (e.meta && typeof e.meta === "object" ? { ...e.meta } : {}) as Record<string, unknown>;
    const r = normRid(e.rid);
    let contact = r ? byRid.get(r) : undefined;
    if (!contact && e.email) contact = byEmail.get(String(e.email).trim().toLowerCase());
    if (r && !contact) meta.rid_raw = r;
    const kind = e.kind ?? (typeof meta.kind === "string" ? (meta.kind as string) : null);
    const mailbox = e.mailbox ?? (typeof meta.mailbox === "string" ? (meta.mailbox as string) : null);
    return {
      rid: contact?.rid ?? null,
      email: contact?.email ?? (e.email ? String(e.email).trim().toLowerCase().slice(0, 254) : null),
      type: String(e.type), ts: normTs(e.ts), kind: kind ? String(kind).slice(0, 32) : null,
      mailbox: mailbox ? String(mailbox).toLowerCase().slice(0, 100) : null,
      meta, dedupe: e.id ? String(e.id).slice(0, 200) : null, test: contact?.is_test ? 1 : 0,
    };
  });

  let accepted = 0, duplicate = 0;
  for (const group of chunks(prepared, 50)) {
    const r = await ingestGroup(db, group);
    accepted += r.accepted;
    duplicate += r.duplicate;
  }
  return { accepted, duplicate, rejected, errors: errors.slice(0, 20) };
}

/**
 * Store a group of events and apply their effects in ONE D1 batch (a transaction): either the events and
 * all their contact/reply updates are stored, or nothing is. A failure therefore leaves nothing half-done,
 * and the producer's retry is applied normally (previously a failure between the insert and the effects
 * left an "accepted" event without its effect, and the retry was then ignored as a duplicate, so a sent
 * contact could stay queued and be sent again). Known dedupe keys are skipped up front; the INSERT has no
 * ON CONFLICT clause, so a concurrent duplicate aborts the batch and the group is re-checked once.
 */
async function ingestGroup(db: D1Database, group: Prepared[]): Promise<{ accepted: number; duplicate: number }> {
  for (let attempt = 0; ; attempt++) {
    const keys = [...new Set(group.map((p) => p.dedupe).filter(Boolean) as string[])];
    const existing = new Set<string>();
    for (const c of chunks(keys, 90)) {
      const { results } = await db.prepare(`SELECT dedupe_key FROM events WHERE dedupe_key IN (${placeholders(c.length)})`).bind(...c).all<{ dedupe_key: string }>();
      for (const r of results) existing.add(r.dedupe_key);
    }
    const seen = new Set<string>();
    const fresh: Prepared[] = [];
    let duplicate = 0;
    for (const p of group) {
      if (p.dedupe && (existing.has(p.dedupe) || seen.has(p.dedupe))) { duplicate++; continue; }
      if (p.dedupe) seen.add(p.dedupe);
      fresh.push(p);
    }
    if (!fresh.length) return { accepted: 0, duplicate };
    const stmts = fresh.flatMap((p) => [
      db.prepare("INSERT INTO events(rid, email, type, kind, mailbox, ts, meta, test, dedupe_key) VALUES (?,?,?,?,?,?,?,?,?)")
        .bind(p.rid, p.email, p.type, p.kind, p.mailbox, p.ts, JSON.stringify(p.meta), p.test, p.dedupe),
      ...effectStatements(db, p),
    ]);
    try {
      await db.batch(stmts);
      return { accepted: fresh.length, duplicate };
    } catch (e) {
      if (attempt === 0 && /UNIQUE/i.test(String((e as Error).message || e))) continue; // raced with an identical event
      throw e;
    }
  }
}

/** SQL side effects of one event. */
function effectStatements(db: D1Database, p: Prepared): D1PreparedStatement[] {
  const out: D1PreparedStatement[] = [];
  const now = nowIso();
  const m = p.meta;
  const clearRes = "reserved_until = NULL, reserved_run = NULL, reserved_kind = NULL";
  if (p.rid) {
    switch (p.type) {
      case "sent":
        if (p.kind === "invite") {
          out.push(db.prepare(`UPDATE contacts SET status = CASE WHEN status = 'queued' THEN 'invited' ELSE status END,
              invited_at = COALESCE(invited_at, ?), invite_msgid = COALESCE(?, invite_msgid), mailbox = COALESCE(?, mailbox),
              ${clearRes}, updated_at = ? WHERE rid = ?`).bind(p.ts, strOrNull(m.message_id), p.mailbox, now, p.rid));
        } else {
          out.push(db.prepare(`UPDATE contacts SET status = CASE WHEN status IN ('invited','reminded') THEN 'reminded' ELSE status END,
              reminded_at = ?, reminder_count = reminder_count + 1, ${clearRes}, updated_at = ? WHERE rid = ?`).bind(p.ts, now, p.rid));
        }
        break;
      case "failed":
        out.push(db.prepare(`UPDATE contacts SET fail_count = fail_count + 1, ${clearRes},
            status = CASE WHEN fail_count + 1 >= 3 AND status = 'queued' THEN 'on_hold' ELSE status END,
            notes = CASE WHEN fail_count + 1 >= 3 AND status = 'queued' THEN trim(COALESCE(notes,'') || ' [auto-hold: 3 send failures]') ELSE notes END,
            updated_at = ? WHERE rid = ?`).bind(now, p.rid));
        break;
      case "bounced_hard":
        out.push(db.prepare(`UPDATE contacts SET bounced = 'hard', ${clearRes},
            status = CASE WHEN status IN ('completed','opted_out','do_not_contact') THEN status ELSE 'bounced' END, updated_at = ? WHERE rid = ?`).bind(now, p.rid));
        break;
      case "bounced_soft":
        out.push(db.prepare(`UPDATE contacts SET soft_bounce_count = soft_bounce_count + 1, bounced = COALESCE(bounced, 'soft'),
            status = CASE WHEN soft_bounce_count + 1 >= 3 AND status IN ('invited','reminded','queued') THEN 'bounced' ELSE status END,
            updated_at = ? WHERE rid = ?`).bind(now, p.rid));
        break;
      case "complaint":
        out.push(db.prepare(`UPDATE contacts SET complained_at = COALESCE(complained_at, ?), status = 'do_not_contact', ${clearRes},
            notes = trim(COALESCE(notes,'') || ' [spam complaint]'), updated_at = ? WHERE rid = ?`).bind(p.ts, now, p.rid));
        break;
      case "opted_out":
        out.push(db.prepare(`UPDATE contacts SET opted_out_at = COALESCE(opted_out_at, ?), ${clearRes},
            status = CASE WHEN status IN ('completed','do_not_contact') THEN status ELSE 'opted_out' END, updated_at = ? WHERE rid = ?`).bind(p.ts, now, p.rid));
        break;
      case "reply":
        out.push(db.prepare("UPDATE contacts SET last_reply_at = ?, updated_at = ? WHERE rid = ?").bind(p.ts, now, p.rid));
        break;
      case "started":
        out.push(db.prepare("UPDATE contacts SET started_at = COALESCE(started_at, ?), updated_at = ? WHERE rid = ?").bind(p.ts, now, p.rid));
        break;
      case "completed":
        out.push(db.prepare(`UPDATE contacts SET completed_at = COALESCE(completed_at, ?), started_at = COALESCE(started_at, ?),
            status = CASE WHEN status = 'do_not_contact' THEN status ELSE 'completed' END, ${clearRes}, updated_at = ? WHERE rid = ?`).bind(p.ts, p.ts, now, p.rid));
        break;
      case "consent_declined":
        out.push(db.prepare(`UPDATE contacts SET consent_declined_at = COALESCE(consent_declined_at, ?), started_at = COALESCE(started_at, ?),
            updated_at = ? WHERE rid = ?`).bind(p.ts, p.ts, now, p.rid));
        break;
      case "click":
        out.push(db.prepare("UPDATE contacts SET first_click_at = COALESCE(first_click_at, ?), click_count = click_count + 1 WHERE rid = ?").bind(p.ts, p.rid));
        break;
      case "released":
        out.push(db.prepare(`UPDATE contacts SET ${clearRes} WHERE rid = ?`).bind(p.rid));
        break;
    }
  }
  // Mail-derived events also appear in the replies inbox (queries need attention; the rest are informational).
  if (["reply", "opted_out", "auto_reply", "complaint"].includes(p.type) && (m.from || m.subject)) {
    const cls = strOrNull(m.classification) || (p.type === "reply" ? "query" : p.type);
    out.push(db.prepare(`INSERT INTO replies(rid, mailbox, from_addr, subject, snippet, received_at, classification, handled, message_id)
        VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(message_id) DO NOTHING`).bind(
      p.rid, p.mailbox, strOrNull(m.from)?.slice(0, 254) ?? null, strOrNull(m.subject)?.slice(0, 300) ?? null,
      strOrNull(m.snippet)?.slice(0, 600) ?? null, p.ts, cls, p.type === "reply" ? 0 : 1,
      strOrNull(m.message_id) ?? p.dedupe));
  }
  return out;
}

function strOrNull(v: unknown): string | null {
  return typeof v === "string" && v.length ? v : null;
}

export async function logJobRun(db: D1Database, job: string, ok: boolean, detail: unknown): Promise<void> {
  await db.prepare("INSERT INTO job_runs(job, ts, ok, detail) VALUES (?,?,?,?)")
    .bind(job.slice(0, 40), nowIso(), ok ? 1 : 0, typeof detail === "string" ? detail.slice(0, 2000) : JSON.stringify(detail ?? null).slice(0, 2000)).run();
}

export async function adminEvent(db: D1Database, actor: string, action: string, meta: Record<string, unknown> = {}, rid: string | null = null): Promise<void> {
  await db.prepare("INSERT INTO events(rid, type, kind, ts, meta) VALUES (?, 'admin', ?, ?, ?)")
    .bind(rid, action.slice(0, 32), nowIso(), JSON.stringify({ actor, ...meta })).run();
}

export async function pauseSending(db: D1Database, reason: string, actor: string): Promise<void> {
  await setSettings(db, { paused: "true", pause_reason: reason.slice(0, 500), paused_at: nowIso(), paused_by: actor });
  await adminEvent(db, actor, "pause", { reason });
}

export async function resumeSending(db: D1Database, actor: string): Promise<void> {
  const row = await db.prepare("SELECT COUNT(*) AS n FROM contacts WHERE complained_at IS NOT NULL AND is_test = 0").first<{ n: number }>();
  await setSettings(db, {
    paused: "false", pause_reason: "", paused_at: "", paused_by: "",
    // Stop rules restart from here so the same old bounces/complaints do not immediately re-pause.
    stop_rule_reset_at: nowIso(), stop_rule_complaint_baseline: String(row?.n ?? 0),
  });
  await adminEvent(db, actor, "resume", {});
}
