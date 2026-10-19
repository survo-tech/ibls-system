/** /api/* : machine API used by the GitHub Actions jobs (Bearer API_TOKEN). */
import { checkBearer } from "./auth";
import { bounceGuard, getSettings, getState, ingestEvents, IncomingEvent, logJobRun, setState, testRecipients } from "./db";
import {
  addDays, configFromSettings, DAILY_HARD_MAX, istMidnightUtc, istParts, reminderCutoffDate, remainingCapacity, stageCap, windowState, isoOf,
  warmupConfigFromSettings,
} from "./schedule";
import { evaluateWarmup, stageLabel, warmupView } from "./warmup";
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
    if (path === "/api/mailboxes" && req.method === "GET") return json({ today_ist: todayIst(), mailboxes: await mailboxStatus(env) });
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
      return json({ day, today, totals, paused: s.paused === "true", pause_reason: s.pause_reason, test_mode: s.test_mode === "true", jobs: lastRuns.results,
        mailboxes: await mailboxStatus(env) });
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
 * Due messages for one mailbox: reminders first (always from the mailbox that sent the invite), then new
 * invites from the SHARED queue in stratified send_order, so the five mailboxes rotate over one queue and a
 * mailbox that is held back by its warm-up never strands contacts. Limited by the mailbox's warm-up cap
 * (never above DAILY_HARD_MAX), hourly cap, even pacing, per-run max and the caller's max.
 * Seeded test contacts stay tied to the mailbox seed_test.py gave them, so every mailbox gets its own seed test.
 * Reservation is one UPDATE … WHERE rid IN (SELECT … LIMIT n) per kind, so concurrent mailboxes cannot
 * double-book a contact. Idempotent per run_id: a repeated call with the same run_id returns the same reservation.
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
  const settingsOut = { postal_address: s.postal_address, from_name: s.from_name, reply_to: s.reply_to, test_mode: testMode, test_recipients: allow };

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
  if (qb["quota_block:" + mailbox] === today) return { messages: [], reason: `${mailbox} hit its sending quota today; resumes tomorrow`, settings: settingsOut };
  const guard = await bounceGuard(db, s, mailbox, now);
  if (guard.blocked) return { messages: [], reason: guard.reason, bounce_guard: guard.counts, settings: settingsOut };

  // 3. Warm-up stage and capacity.
  const wc = warmupConfigFromSettings(s);
  const wu = await evaluateWarmup(db, s, mailbox, now, true);
  const seedOnly = !testMode && wu.stage === 0;
  if (seedOnly && !allow.length) {
    return { messages: [], reason: `${mailbox} is at warm-up stage 0 (seed test) and test_recipients is empty: run make seed-test, or set the stage on the dashboard`, warmup: { stage: 0 }, settings: settingsOut };
  }
  // In test mode the seed cap is the minimum, so a test run works whatever the stage.
  const dailyCap = testMode ? Math.max(stageCap(wu.stage, wc), stageCap(0, wc)) : stageCap(wu.stage, wc);
  const dayStart = istMidnightUtc(today);
  const hourAgo = isoOf(new Date(now.getTime() - 3600_000));
  const counts = await db.prepare(`SELECT
      (SELECT COUNT(*) FROM events WHERE mailbox = ?1 AND ts >= ?2 AND (type = 'sent' OR (type = 'bounced_hard' AND kind = 'smtp'))) AS sent_today,
      (SELECT COUNT(*) FROM events WHERE mailbox = ?1 AND ts >= ?3 AND (type = 'sent' OR (type = 'bounced_hard' AND kind = 'smtp'))) AS sent_hour,
      (SELECT COUNT(*) FROM contacts WHERE reserved_until > ?4 AND mailbox = ?1) AS reserved`)
    .bind(mailbox, dayStart, hourAgo, nowS).first<{ sent_today: number; sent_hour: number; reserved: number }>();
  const room = remainingCapacity({
    dailyCap, sentToday: counts?.sent_today || 0, sentLastHour: counts?.sent_hour || 0, reservedActive: counts?.reserved || 0,
    hourlyCap: Math.min(cfg.hourlyCap, DAILY_HARD_MAX), perRunMax: Number(s.per_run_max) || 20, requested,
    pace: !testMode && !seedOnly && s.pace_evenly === "true" && win.open ? { minutesInto: win.minutesInto, windowMinutes: win.windowMinutes } : null,
  });
  const capInfo = {
    stage: wu.stage, stage_label: stageLabel(wu.stage, stageCap(wu.stage, wc)), daily_cap: dailyCap, sent_today: counts?.sent_today || 0,
    sent_last_hour: counts?.sent_hour || 0, reserved: counts?.reserved || 0, room, warmup_last: wu.last || null,
  };
  if (room <= 0) return { messages: [], reason: "cap reached for now", caps: capInfo, settings: settingsOut };

  // 4. Reserve candidates (reminders first). Every statement binds the same 7 parameters:
  //    ?1 mailbox, ?2 now, ?3 reminder cutoff, ?4 limit, ?5 allow-list JSON, ?6 reserved_until, ?7 run id.
  const audience = testMode ? "AND lower(email) IN (SELECT value FROM json_each(?5))"
    : seedOnly ? "AND is_test = 1 AND lower(email) IN (SELECT value FROM json_each(?5))"
    : "AND is_test = 0 AND ?5 = ?5";
  const free = "AND (reserved_until IS NULL OR reserved_until < ?2)";
  const eligibleForReminder = `AND completed_at IS NULL AND opted_out_at IS NULL AND complained_at IS NULL
      AND consent_declined_at IS NULL AND COALESCE(bounced,'') <> 'hard'`;
  const cut1 = istMidnightUtc(addDays(reminderCutoffDate(today, cfg.reminderAfterWorkdays, cfg), 1));
  const cut2 = istMidnightUtc(addDays(reminderCutoffDate(today, cfg.reminder2AfterWorkdays, cfg), 1));
  const until = isoOf(new Date(now.getTime() + RESERVATION_MINUTES * 60000));
  const allowJson = JSON.stringify(allow);
  let taken = 0;
  const take = async (kind: Msg["kind"], where: string, order: string, extra: string, assignMailbox: boolean) => {
    const left = room - taken;
    if (left <= 0) return;
    const sql = `UPDATE contacts SET reserved_until = ?6, reserved_run = ?7, reserved_kind = '${kind}'${assignMailbox ? ", mailbox = ?1" : ""}
        WHERE rid IN (SELECT rid FROM contacts WHERE ${where} ${free} ${audience} ${eligibleForReminderIf(kind, eligibleForReminder)}
                      ORDER BY ${order} LIMIT ?4)
          AND (reserved_until IS NULL OR reserved_until < ?2)`;
    const r = await db.prepare(sql).bind(mailbox, nowS, extra, left, allowJson, until, runId).run();
    taken += r.meta.changes ?? 0;
  };
  await take("reminder", "mailbox = ?1 AND status = 'invited' AND reminder_count = 0 AND invited_at < ?3", "invited_at, send_order", cut1, false);
  if (cfg.reminder2Enabled) {
    await take("reminder2", "mailbox = ?1 AND status = 'reminded' AND reminder_count = 1 AND reminded_at < ?3", "reminded_at, send_order", cut2, false);
  }
  // Invites: shared queue for real contacts; seeded test contacts only from their own mailbox. (?3 unused here.)
  await take("invite", "status = 'queued' AND (is_test = 0 OR mailbox = ?1) AND ?3 = ?3", "send_order, rid", "x", true);
  if (!taken) return { messages: [], reason: seedOnly ? `${mailbox} is at warm-up stage 0 (seed test): no seed contacts left; run make seed-test, or set the stage on the dashboard` : "nothing due", caps: capInfo, settings: settingsOut };
  const messages = await reservedFor(db, runId, nowS, env);
  return { messages, run_id: runId, reserved_until: until, caps: capInfo, settings: settingsOut };
}

function eligibleForReminderIf(kind: string, clause: string): string {
  return kind === "invite" ? "" : clause;
}

/** Warm-up stage, cap and today's sends for every mailbox (job API and dashboard). */
export async function mailboxStatus(env: Env, now = new Date(), persist = false) {
  const s = await getSettings(env.DB);
  const out = [];
  for (const mb of mailboxes(env)) out.push(await warmupView(env.DB, s, mb, now, persist));
  return out;
}

async function reservedFor(db: D1Database, runId: string, nowS: string, env: Env): Promise<Msg[]> {
  const { results } = await db.prepare(`SELECT rid, email, salutation, company, peer_tier, mailbox, reserved_kind AS kind, invite_msgid
      FROM contacts WHERE reserved_run = ? AND reserved_until > ? ORDER BY CASE reserved_kind WHEN 'invite' THEN 1 ELSE 0 END, send_order`)
    .bind(runId, nowS).all<Omit<Msg, "survey_link" | "optout_link">>();
  const origin = env.SITE_ORIGIN.replace(/\/$/, "");
  return results.map((r) => ({ ...r, survey_link: `${origin}/s/${r.rid}`, optout_link: `${origin}/optout/${r.rid}` }));
}

