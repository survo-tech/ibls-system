/** /api/* : machine API used by the GitHub Actions jobs (Bearer API_TOKEN). */
import { checkBearer } from "./auth";
import { bounceGuard, getSettings, getState, ingestEvents, IncomingEvent, logJobRun, setState, testRecipients } from "./db";
import {
  addDays, configFromSettings, istMidnightUtc, istParts, rampCap, reminderCutoffDate, remainingCapacity, windowState, isoOf,
} from "./schedule";
import { kpis, rollupDay, todayIst } from "./stats";
import { Env, json, mailboxes, nowIso, randomId } from "./util";

const RESERVATION_MINUTES = 30;

export async function handleApi(req: Request, env: Env, url: URL): Promise<Response> {
  if (!checkBearer(req, env)) return json({ error: "unauthorized" }, 401);
  const path = url.pathname.replace(/\/+$/, "");
  const db = env.DB;
  try {
    if (path === "/api/settings" && req.method === "GET") {
      const s = await getSettings(db);
      const cfg = configFromSettings(s);
      return json({ settings: s, now: nowIso(), today_ist: todayIst(), window: windowState(new Date(), cfg), mailboxes: mailboxes(env) });
    }
    if (path === "/api/next-batch" && req.method === "GET") return json(await nextBatch(env, url));
    if (path === "/api/events" && req.method === "POST") {
      const body = (await req.json()) as { events?: IncomingEvent[] } | IncomingEvent[];
      const events = Array.isArray(body) ? body : body.events || [];
      if (events.length > 1000) return json({ error: "max 1000 events per request" }, 413);
      return json(await ingestEvents(db, events));
    }
    if (path === "/api/release" && req.method === "POST") {
      const body = (await req.json()) as { run_id?: string; rids?: string[] };
      if (!body.run_id) return json({ error: "run_id required" }, 400);
      const r = await db.prepare("UPDATE contacts SET reserved_until = NULL, reserved_run = NULL, reserved_kind = NULL WHERE reserved_run = ?").bind(body.run_id).run();
      return json({ released: r.meta.changes ?? 0 });
    }
    if (path === "/api/job-run" && req.method === "POST") {
      const body = (await req.json()) as { job?: string; ok?: boolean; detail?: unknown; quota_mailbox?: string };
      if (!body.job) return json({ error: "job required" }, 400);
      await logJobRun(db, body.job, body.ok !== false, body.detail ?? null);
      if (body.quota_mailbox) await setState(db, { ["quota_block:" + body.quota_mailbox.toLowerCase()]: todayIst() });
      return json({ ok: true });
    }
    if (path === "/api/state" && req.method === "GET") {
      const keys = (url.searchParams.get("keys") || "").split(",").map((k) => k.trim()).filter(Boolean).slice(0, 50);
      return json({ state: await getState(db, keys) });
    }
    if (path === "/api/state" && (req.method === "PUT" || req.method === "POST")) {
      const body = (await req.json()) as Record<string, unknown>;
      const kv: Record<string, string> = {};
      for (const [k, v] of Object.entries(body).slice(0, 50)) if (/^[\w:@.\-]{1,120}$/.test(k)) kv[k] = String(v).slice(0, 4000);
      await setState(db, kv);
      return json({ ok: true, saved: Object.keys(kv).length });
    }
    if (path === "/api/summary" && req.method === "GET") {
      const day = url.searchParams.get("day") || todayIst();
      const s = await getSettings(db);
      const [today, totals] = await Promise.all([rollupDay(db, day), kpis(db)]);
      const lastRuns = await db.prepare("SELECT job, ts, ok, detail FROM job_runs WHERE id IN (SELECT MAX(id) FROM job_runs GROUP BY job)").all();
      return json({ day, today, totals, paused: s.paused === "true", pause_reason: s.pause_reason, test_mode: s.test_mode === "true", jobs: lastRuns.results });
    }
    return json({ error: "not found" }, 404);
  } catch (e) {
    return json({ error: String((e as Error).message || e) }, 500);
  }
}

interface Msg {
  rid: string; email: string; salutation: string; company: string; peer_tier: string; mailbox: string;
  kind: "invite" | "reminder" | "reminder2"; invite_msgid: string | null; survey_link: string; optout_link: string;
}

/**
 * Due messages for one mailbox: reminders first, then new invites in stratified send_order,
 * limited by daily ramp cap, hourly cap, even pacing, per-run max and the caller's max.
 * Idempotent per run_id: a repeated call with the same run_id returns the same reservation.
 */
export async function nextBatch(env: Env, url: URL) {
  const db = env.DB;
  const mailbox = (url.searchParams.get("mailbox") || "").toLowerCase();
  if (!mailboxes(env).includes(mailbox)) return { messages: [], reason: `unknown mailbox ${mailbox}` };
  const requested = Math.max(0, Math.min(200, Number(url.searchParams.get("max") || "20") || 0));
  const runId = (url.searchParams.get("run_id") || randomId("run-")).slice(0, 80) + ":" + mailbox;
  // Local testing only: ALLOW_TIME_OVERRIDE=1 (set in .dev.vars, never in production) lets ?now= fake the clock.
  const override = env.ALLOW_TIME_OVERRIDE === "1" ? url.searchParams.get("now") : null;
  const now = override && !Number.isNaN(Date.parse(override)) ? new Date(override) : new Date();
  const nowS = isoOf(now);
  const s = await getSettings(db);
  const cfg = configFromSettings(s);
  const testMode = s.test_mode === "true";
  const allow = testRecipients(s);
  const settingsOut = { postal_address: s.postal_address, from_name: s.from_name, test_mode: testMode, test_recipients: allow };

  // 1. Idempotency: same run asking again gets the same reservation.
  const existing = await reservedFor(db, runId, nowS, env);
  if (existing.length) return { messages: existing, run_id: runId, settings: settingsOut, idempotent: true };

  // 2. Global gates.
  if (s.paused === "true") return { messages: [], reason: `paused: ${s.pause_reason || "no reason given"}`, paused: true, settings: settingsOut };
  const win = windowState(now, cfg);
  if (!testMode && !win.open) return { messages: [], reason: win.reason, settings: settingsOut };
  if (testMode && !allow.length) return { messages: [], reason: "test_mode is on and test_recipients is empty", settings: settingsOut };
  const today = istParts(now).date;
  const qb = await getState(db, ["quota_block:" + mailbox]);
  if (qb["quota_block:" + mailbox] === today) return { messages: [], reason: `${mailbox} hit an SMTP quota today; resumes tomorrow`, settings: settingsOut };
  const guard = await bounceGuard(db, s, mailbox, now);
  if (guard.blocked) return { messages: [], reason: guard.reason, bounce_guard: guard.counts, settings: settingsOut };

  // 3. Capacity.
  const dayStart = istMidnightUtc(today);
  const hourAgo = isoOf(new Date(now.getTime() - 3600_000));
  const counts = await db.prepare(`SELECT
      (SELECT COUNT(DISTINCT substr(datetime(ts, '+330 minutes'), 1, 10)) FROM events
         WHERE type = 'sent' AND mailbox = ?1 AND test = 0 AND ts < ?2) AS days_used,
      (SELECT COUNT(*) FROM events WHERE mailbox = ?1 AND ts >= ?2 AND (type = 'sent' OR (type = 'bounced_hard' AND kind = 'smtp'))) AS sent_today,
      (SELECT COUNT(*) FROM events WHERE mailbox = ?1 AND ts >= ?3 AND (type = 'sent' OR (type = 'bounced_hard' AND kind = 'smtp'))) AS sent_hour,
      (SELECT COUNT(*) FROM contacts WHERE mailbox = ?1 AND reserved_until > ?4) AS reserved`)
    .bind(mailbox, dayStart, hourAgo, nowS).first<{ days_used: number; sent_today: number; sent_hour: number; reserved: number }>();
  const k = (counts?.days_used || 0) + 1;
  const dailyCap = rampCap(k, cfg.ramp, cfg.capMax);
  const room = remainingCapacity({
    dailyCap, sentToday: counts?.sent_today || 0, sentLastHour: counts?.sent_hour || 0, reservedActive: counts?.reserved || 0,
    hourlyCap: cfg.hourlyCap, perRunMax: Number(s.per_run_max) || 20, requested,
    pace: !testMode && s.pace_evenly === "true" && win.open ? { minutesInto: win.minutesInto, windowMinutes: win.windowMinutes } : null,
  });
  const capInfo = { send_day: k, daily_cap: dailyCap, sent_today: counts?.sent_today || 0, sent_last_hour: counts?.sent_hour || 0, reserved: counts?.reserved || 0, room };
  if (room <= 0) return { messages: [], reason: "cap reached for now", caps: capInfo, settings: settingsOut };

  // 4. Candidate selection (reminders first).
  // ?1 mailbox, ?2 now, ?3 cutoff, ?4 limit, ?5 test allow-list (JSON) when in test mode.
  const audience = testMode ? "AND lower(email) IN (SELECT value FROM json_each(?5))" : "AND is_test = 0";
  const free = "AND (reserved_until IS NULL OR reserved_until < ?2)";
  const eligibleForReminder = `AND completed_at IS NULL AND opted_out_at IS NULL AND complained_at IS NULL
      AND consent_declined_at IS NULL AND COALESCE(bounced,'') <> 'hard'`;
  const cut1 = istMidnightUtc(addDays(reminderCutoffDate(today, cfg.reminderAfterWorkdays, cfg), 1));
  const cut2 = istMidnightUtc(addDays(reminderCutoffDate(today, cfg.reminder2AfterWorkdays, cfg), 1));
  const picks: { rid: string; kind: Msg["kind"] }[] = [];
  const allowJson = JSON.stringify(allow);
  const take = async (sql: string, kind: Msg["kind"], extra: string) => {
    const left = room - picks.length;
    if (left <= 0) return;
    const { results } = await db.prepare(sql.replace("{AUD}", audience).replace("{FREE}", free).replace("{ELIG}", eligibleForReminder))
      .bind(...(testMode ? [mailbox, nowS, extra, left, allowJson] : [mailbox, nowS, extra, left])).all<{ rid: string }>();
    for (const r of results) picks.push({ rid: r.rid, kind });
  };
  await take(`SELECT rid FROM contacts WHERE mailbox = ?1 AND status = 'invited' AND reminder_count = 0 AND invited_at < ?3
      {ELIG} {FREE} {AUD} ORDER BY invited_at, send_order LIMIT ?4`, "reminder", cut1);
  if (cfg.reminder2Enabled) {
    await take(`SELECT rid FROM contacts WHERE mailbox = ?1 AND status = 'reminded' AND reminder_count = 1 AND reminded_at < ?3
        {ELIG} {FREE} {AUD} ORDER BY reminded_at, send_order LIMIT ?4`, "reminder2", cut2);
  }
  // (?3 is unused for invites but must appear so the bound-parameter count matches.)
  await take(`SELECT rid FROM contacts WHERE mailbox = ?1 AND status = 'queued' AND ?3 = ?3
      {FREE} {AUD} ORDER BY send_order, rid LIMIT ?4`, "invite", "x");
  if (!picks.length) return { messages: [], reason: "nothing due", caps: capInfo, settings: settingsOut };

  // 5. Reserve (only rows still free, so concurrent callers cannot double-book).
  const until = isoOf(new Date(now.getTime() + RESERVATION_MINUTES * 60000));
  await db.batch(picks.map((p) => db.prepare(`UPDATE contacts SET reserved_until = ?, reserved_run = ?, reserved_kind = ?
      WHERE rid = ? AND (reserved_until IS NULL OR reserved_until < ?)`).bind(until, runId, p.kind, p.rid, nowS)));
  const messages = await reservedFor(db, runId, nowS, env);
  return { messages, run_id: runId, reserved_until: until, caps: capInfo, settings: settingsOut };
}

async function reservedFor(db: D1Database, runId: string, nowS: string, env: Env): Promise<Msg[]> {
  const { results } = await db.prepare(`SELECT rid, email, salutation, company, peer_tier, mailbox, reserved_kind AS kind, invite_msgid
      FROM contacts WHERE reserved_run = ? AND reserved_until > ? ORDER BY CASE reserved_kind WHEN 'invite' THEN 1 ELSE 0 END, send_order`)
    .bind(runId, nowS).all<Omit<Msg, "survey_link" | "optout_link">>();
  const origin = env.SITE_ORIGIN.replace(/\/$/, "");
  return results.map((r) => ({ ...r, survey_link: `${origin}/s/${r.rid}`, optout_link: `${origin}/optout/${r.rid}` }));
}

