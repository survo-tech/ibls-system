/** /dashboard and /dashboard/api/* (researcher UI). Auth is enforced by the caller (index.ts). */
import DASHBOARD_HTML from "../dashboard/dashboard.html";
import { evaluateStopRules } from "./cron";
import { adminEvent, bounceGuard, EDITABLE, getSettings, getState, pauseSending, resumeSending, setSettings } from "./db";
import { configFromSettings, DAILY_HARD_MAX, istMidnightUtc, istParts, MailboxProjectionInput, projectSchedule, windowState, isoOf } from "./schedule";
import { setWarmupStage, warmupView, WarmupView } from "./warmup";
import { kpis, rollupDay } from "./stats";
import { csvCell, Env, htmlResponse, json, mailboxes, normRid, nowIso } from "./util";

export async function handleDashboard(req: Request, env: Env, url: URL, actor: string): Promise<Response> {
  const path = url.pathname.replace(/\/+$/, "");
  if (path === "/dashboard" && req.method === "GET") {
    return htmlResponse(DASHBOARD_HTML, 200, {
      "x-frame-options": "DENY",
      "content-security-policy": "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
    });
  }
  if (!path.startsWith("/dashboard/api/")) return json({ error: "not found" }, 404);
  // CSRF guard for state-changing calls: browsers cannot add this header cross-site without CORS.
  if (req.method !== "GET" && req.headers.get("x-ibls-dashboard") !== "1") return json({ error: "missing X-IBLS-Dashboard header" }, 403);
  const db = env.DB;
  const route = path.slice("/dashboard/api/".length);
  try {
    if (route === "whoami") return json({ actor });
    if (route === "summary" && req.method === "GET") return json(await summary(env));
    if (route === "daily" && req.method === "GET") {
      await rollupDay(db, istParts(new Date()).date);
      const { results } = await db.prepare("SELECT * FROM daily_stats ORDER BY day").all();
      return json({ days: results });
    }
    if (route === "strata" && req.method === "GET") {
      const dims = ["region", "size_band", "nature", "email_tier"];
      const sql = dims.map((d) => `SELECT '${d}' AS dim, COALESCE(${d}, '(blank)') AS value, COUNT(*) AS contacts,
          SUM(status = 'on_hold') AS on_hold, SUM(invited_at IS NOT NULL) AS invited, SUM(first_click_at IS NOT NULL) AS clicked,
          SUM(started_at IS NOT NULL) AS started, SUM(completed_at IS NOT NULL) AS completed, SUM(bounced = 'hard') AS bounced,
          SUM(opted_out_at IS NOT NULL) AS opted_out FROM contacts WHERE is_test = 0 GROUP BY ${d}`).join(" UNION ALL ");
      const { results } = await db.prepare(sql).all();
      return json({ rows: results });
    }
    if (route === "batches" && req.method === "GET") {
      const { results } = await db.prepare(`SELECT substr(datetime(invited_at, '+330 minutes'), 1, 10) AS day, COUNT(*) AS invited,
          SUM(reminder_count > 0) AS reminded, SUM(first_click_at IS NOT NULL) AS clicked, SUM(started_at IS NOT NULL) AS started,
          SUM(completed_at IS NOT NULL) AS completed, SUM(bounced = 'hard') AS hard, SUM(bounced = 'soft') AS soft,
          SUM(opted_out_at IS NOT NULL) AS opted_out, SUM(complained_at IS NOT NULL) AS complaints
        FROM contacts WHERE invited_at IS NOT NULL AND is_test = 0 GROUP BY day ORDER BY day DESC`).all<Record<string, unknown>>();
      const per = await db.prepare(`SELECT substr(datetime(invited_at, '+330 minutes'), 1, 10) AS day, mailbox, COUNT(*) AS n
          FROM contacts WHERE invited_at IS NOT NULL AND is_test = 0 GROUP BY day, mailbox`).all<{ day: string; mailbox: string | null; n: number }>();
      const by: Record<string, Record<string, number>> = {};
      for (const r of per.results) (by[r.day] ||= {})[r.mailbox || "?"] = r.n;
      return json({ mailboxes: mailboxes(env), rows: results.map((r) => ({ ...r, by_mailbox: by[String(r.day)] || {} })) });
    }
    if (route === "activity" && req.method === "GET") {
      const limit = Math.min(200, Number(url.searchParams.get("limit")) || 60);
      const { results } = await db.prepare(`SELECT e.id, e.ts, e.type, e.kind, e.rid, e.mailbox, e.meta, e.test, c.company
          FROM events e LEFT JOIN contacts c ON c.rid = e.rid ORDER BY e.id DESC LIMIT ?`).bind(limit).all();
      return json({ rows: results });
    }
    if (route === "search" && req.method === "GET") {
      const q = (url.searchParams.get("q") || "").trim();
      if (q.length < 2) return json({ rows: [] });
      const like = `%${q.replace(/[%_]/g, "")}%`;
      const { results } = await db.prepare(`SELECT rid, email, company, status, region, size_band, nature, mailbox, invited_at, reminded_at,
          first_click_at, completed_at, is_test FROM contacts WHERE rid = ? OR email LIKE ? OR company LIKE ? ORDER BY company LIMIT 50`)
        .bind(q.toUpperCase(), like, like).all();
      return json({ rows: results });
    }
    if (route.startsWith("contact/")) {
      const parts = route.split("/");
      const rid = normRid(parts[1]);
      if (!rid) return json({ error: "bad rid" }, 400);
      if (parts.length === 2 && req.method === "GET") {
        const contact = await db.prepare("SELECT * FROM contacts WHERE rid = ?").bind(rid).first();
        if (!contact) return json({ error: "not found" }, 404);
        const ev = await db.prepare("SELECT id, ts, type, kind, mailbox, meta FROM events WHERE rid = ? ORDER BY ts, id").bind(rid).all();
        const rp = await db.prepare("SELECT * FROM replies WHERE rid = ? ORDER BY received_at").bind(rid).all();
        return json({ contact, events: ev.results, replies: rp.results });
      }
      if (parts[2] === "status" && req.method === "POST") {
        const body = (await req.json()) as { status?: string; note?: string };
        return json(await setContactStatus(db, rid, String(body.status || ""), String(body.note || "").slice(0, 300), actor));
      }
    }
    if (route === "replies" && req.method === "GET") {
      const filter = url.searchParams.get("filter") || "open";
      const where = filter === "all" ? "1=1" : filter === "queries" ? "r.classification = 'query'" : "r.handled = 0";
      const { results } = await db.prepare(`SELECT r.*, c.company FROM replies r LEFT JOIN contacts c ON c.rid = r.rid
          WHERE ${where} ORDER BY r.handled, r.received_at DESC LIMIT 300`).all();
      return json({ rows: results });
    }
    if (route.startsWith("replies/") && req.method === "POST") {
      const id = Number(route.split("/")[1]);
      const body = (await req.json()) as { handled?: boolean };
      const h = body.handled === false ? 0 : 1;
      await db.prepare("UPDATE replies SET handled = ?, handled_by = ?, handled_at = ? WHERE id = ?").bind(h, h ? actor : null, h ? nowIso() : null, id).run();
      return json({ ok: true });
    }
    if (route === "pause" && req.method === "POST") {
      const body = (await req.json()) as { reason?: string };
      const reason = String(body.reason || "").trim();
      if (!reason) return json({ error: "a reason is required" }, 400);
      await pauseSending(db, `${reason} (by ${actor})`, actor);
      return json({ ok: true });
    }
    if (route === "mailboxes" && req.method === "GET") {
      const s = await getSettings(db);
      const out = [];
      for (const mb of mailboxes(env)) out.push(await warmupView(db, s, mb, new Date(), false));
      return json({ mailboxes: out, hard_max: DAILY_HARD_MAX });
    }
    if (route === "warmup" && req.method === "POST") {
      const body = (await req.json()) as { mailbox?: string; stage?: number | string; note?: string };
      const mb = String(body.mailbox || "").toLowerCase();
      const stage = Number(body.stage);
      if (!mailboxes(env).includes(mb)) return json({ error: "unknown mailbox" }, 400);
      if (!Number.isInteger(stage) || stage < 0 || stage > 20) return json({ error: "stage must be a whole number (0 = seed test)" }, 400);
      const s = await getSettings(db);
      const st = await setWarmupStage(db, s, mb, stage, actor, String(body.note || "").slice(0, 200));
      await adminEvent(db, actor, "warmup_stage", { mailbox: mb, stage: st.stage, note: body.note || "" });
      return json({ ok: true, mailbox: mb, stage: st.stage });
    }
    if (route === "resume" && req.method === "POST") {
      await resumeSending(db, actor);
      return json({ ok: true });
    }
    if (route === "settings" && req.method === "GET") return json({ settings: await getSettings(db), editable: Object.keys(EDITABLE) });
    if (route === "settings" && req.method === "POST") {
      const body = (await req.json()) as Record<string, unknown>;
      const changes: Record<string, string> = {};
      const bad: string[] = [];
      for (const [k, v] of Object.entries(body)) {
        const val = String(v ?? "").trim();
        if (!EDITABLE[k] || !EDITABLE[k](val)) bad.push(k); else changes[k] = val;
      }
      if (bad.length) return json({ error: `invalid or non-editable: ${bad.join(", ")}` }, 400);
      if (changes.test_recipients !== undefined) changes.test_recipients = changes.test_recipients.split(/[\s,;]+/).filter(Boolean).map((e) => e.toLowerCase()).join(",");
      const before = await getSettings(db);
      await setSettings(db, changes);
      const diff: Record<string, [string, string]> = {};
      for (const [k, v] of Object.entries(changes)) if (before[k] !== v) diff[k] = [before[k], v];
      if (Object.keys(diff).length) await adminEvent(db, actor, "settings", { diff });
      return json({ ok: true, changed: Object.keys(diff) });
    }
    if (route === "export.csv" && req.method === "GET") return exportCsv(db);
    return json({ error: "not found" }, 404);
  } catch (e) {
    return json({ error: String((e as Error).message || e) }, 500);
  }
}

async function setContactStatus(db: D1Database, rid: string, status: string, note: string, actor: string) {
  const c = await db.prepare("SELECT status, invited_at, reminder_count, opted_out_at, complained_at, bounced FROM contacts WHERE rid = ?").bind(rid)
    .first<{ status: string; invited_at: string | null; reminder_count: number; opted_out_at: string | null; complained_at: string | null; bounced: string | null }>();
  if (!c) return { error: "not found" };
  let next: string;
  if (status === "on_hold" || status === "do_not_contact") next = status;
  else if (status === "active") {
    if (!["on_hold", "do_not_contact"].includes(c.status)) return { error: `contact is ${c.status}; only on_hold/do_not_contact can be re-activated` };
    if (c.opted_out_at || c.complained_at || c.bounced === "hard") return { error: "contact opted out, complained or hard-bounced; cannot re-activate" };
    next = !c.invited_at ? "queued" : c.reminder_count > 0 ? "reminded" : "invited";
  } else return { error: "status must be on_hold, do_not_contact or active" };
  const tag = `[${nowIso().slice(0, 10)} ${actor}: ${next}${note ? " – " + note : ""}]`;
  await db.prepare(`UPDATE contacts SET status = ?, notes = trim(COALESCE(notes,'') || ' ' || ?), reserved_until = NULL, reserved_run = NULL,
      reserved_kind = NULL, updated_at = ? WHERE rid = ?`).bind(next, tag, nowIso(), rid).run();
  await adminEvent(db, actor, "contact_status", { from: c.status, to: next, note }, rid);
  return { ok: true, status: next };
}

async function summary(env: Env) {
  const db = env.DB;
  const s = await getSettings(db);
  const cfg = configFromSettings(s);
  const now = new Date();
  const today = istParts(now).date;
  const dayStart = istMidnightUtc(today);
  const k = await kpis(db);

  // Projection inputs per mailbox (invites come from one shared queue; reminders stay with their mailbox).
  const boxes: MailboxProjectionInput[] = [];
  const warm: Record<string, WarmupView> = {};
  for (const mb of mailboxes(env)) warm[mb] = await warmupView(db, s, mb, now, false);
  for (const mb of mailboxes(env)) {
    const c = await db.prepare(`SELECT
        (SELECT COUNT(DISTINCT substr(datetime(ts, '+330 minutes'), 1, 10)) FROM events WHERE type='sent' AND mailbox=?1 AND test=0 AND ts < ?2) AS used,
        (SELECT COUNT(*) FROM events WHERE type='sent' AND mailbox=?1 AND ts >= ?2) AS today,
        (SELECT COUNT(*) FROM contacts WHERE mailbox=?1 AND status='queued' AND is_test=0) AS queued`).bind(mb, dayStart)
      .first<{ used: number; today: number; queued: number }>();
    const elig = "completed_at IS NULL AND opted_out_at IS NULL AND complained_at IS NULL AND consent_declined_at IS NULL AND is_test=0";
    const r1 = await db.prepare(`SELECT substr(datetime(invited_at,'+330 minutes'),1,10) AS d, COUNT(*) AS n FROM contacts
        WHERE mailbox=? AND status='invited' AND reminder_count=0 AND ${elig} GROUP BY d`).bind(mb).all<{ d: string; n: number }>();
    const r2 = cfg.reminder2Enabled ? await db.prepare(`SELECT substr(datetime(reminded_at,'+330 minutes'),1,10) AS d, COUNT(*) AS n FROM contacts
        WHERE mailbox=? AND status='reminded' AND reminder_count=1 AND ${elig} GROUP BY d`).bind(mb).all<{ d: string; n: number }>() : { results: [] };
    boxes.push({
      mailbox: mb, sendDaysUsed: c?.used || 0, sentToday: c?.today || 0, queued: c?.queued || 0, stage: warm[mb].stage,
      pendingReminders: Object.fromEntries(r1.results.map((r) => [r.d, r.n])),
      pendingReminders2: Object.fromEntries((r2.results as { d: string; n: number }[]).map((r) => [r.d, r.n])),
    });
  }
  const done = (k.completed || 0) + (k.bounced_hard || 0) + (k.opted_out || 0);
  const remindRate = k.invited ? Math.min(1, Math.max(0.6, 1 - done / k.invited)) : 0.92;
  const sharedQueued = (await db.prepare("SELECT COUNT(*) AS n FROM contacts WHERE status = 'queued' AND is_test = 0").first<{ n: number }>())?.n || 0;
  const projection = projectSchedule(now, cfg, boxes, remindRate, sharedQueued);

  const jobs = await db.prepare("SELECT job, ts, ok, detail FROM job_runs WHERE id IN (SELECT MAX(id) FROM job_runs GROUP BY job) ORDER BY job").all();
  const lastErrors = await db.prepare("SELECT job, ts, detail FROM job_runs WHERE ok = 0 AND id IN (SELECT MAX(id) FROM job_runs WHERE ok = 0 GROUP BY job)").all();
  const state = await getState(db, ["cron_last", ...mailboxes(env).map((m) => "quota_block:" + m)]);
  const stop = await evaluateStopRules(db, s, now);
  const guards: Record<string, unknown> = {};
  for (const mb of mailboxes(env)) guards[mb] = await bounceGuard(db, s, mb, now);
  return {
    now: isoOf(now), today_ist: today, kpis: k,
    funnel: { invited: k.invited || 0, clicked: k.clicked_human || 0, started: k.started || 0, completed: k.completed || 0 },
    schedule: {
      projection, remind_rate_assumed: remindRate, target_end_date: s.target_end_date, window: windowState(now, cfg),
      shared_queue: sharedQueued, hard_max_per_mailbox: DAILY_HARD_MAX,
      mailboxes: boxes.map((b) => ({ mailbox: b.mailbox, send_days_used: b.sendDaysUsed, sent_today: warm[b.mailbox].sent_today,
        stage: warm[b.mailbox].stage, stage_label: warm[b.mailbox].stage_label, daily_cap: warm[b.mailbox].daily_cap,
        remaining_today: warm[b.mailbox].remaining_today, warmup_last: warm[b.mailbox].last,
        awaiting_reminder: Object.values(b.pendingReminders).reduce((a, n) => a + n, 0) })),
    },
    health: {
      paused: s.paused === "true", pause_reason: s.pause_reason, paused_at: s.paused_at || "", paused_by: s.paused_by || "",
      test_mode: s.test_mode === "true", test_recipients: s.test_recipients,
      jobs: jobs.results, last_errors: lastErrors.results, cron_last: safeJson(state.cron_last),
      quota_blocks: Object.fromEntries(Object.entries(state).filter(([key, v]) => key.startsWith("quota_block:") && v === today)),
      stop_rules: stop.checks, stop_rule_would_pause: stop.pause ? stop.reason : null, bounce_guard: guards,
    },
  };
}

function safeJson(s: string | undefined) {
  try { return s ? JSON.parse(s) : null; } catch { return s; }
}

async function exportCsv(db: D1Database): Promise<Response> {
  const cols = ["rid", "email", "salutation", "director_name", "company", "cin", "state", "region", "size_band", "nature", "peer_tier", "email_tier",
    "mailbox", "status", "send_order", "is_test", "invited_at", "reminded_at", "reminder_count", "first_click_at", "click_count", "started_at",
    "completed_at", "consent_declined_at", "bounced", "soft_bounce_count", "complained_at", "opted_out_at", "last_reply_at", "notes"];
  const { results } = await db.prepare(`SELECT ${cols.join(", ")} FROM contacts ORDER BY send_order, rid`).all<Record<string, unknown>>();
  const lines = [cols.join(",")];
  for (const r of results) lines.push(cols.map((c) => csvCell(r[c])).join(","));
  return new Response("\ufeff" + lines.join("\r\n") + "\r\n", {
    headers: {
      "content-type": "text/csv; charset=utf-8",
      "content-disposition": `attachment; filename="ibls-contacts-${isoOf(new Date()).slice(0, 10)}.csv"`,
      "cache-control": "no-store",
    },
  });
}
