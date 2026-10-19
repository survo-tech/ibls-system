/** Aggregate queries shared by the dashboard, the daily report and the cron rollup. Test contacts are excluded. */
import { addDays, istMidnightUtc, istParts } from "./schedule";
import { nowIso } from "./util";

export interface DayStats {
  day: string; invites: number; reminders: number; failed: number; hard: number; soft: number; clicks: number;
  started: number; completed: number; declined: number; opted_out: number; complaints: number; replies: number;
}

/** Compute one IST day's numbers from events and upsert them into daily_stats. */
export async function rollupDay(db: D1Database, day: string): Promise<DayStats> {
  const start = istMidnightUtc(day), end = istMidnightUtc(addDays(day, 1));
  const r = await db.prepare(`SELECT
      COALESCE(SUM(type='sent' AND kind='invite'),0) AS invites,
      COALESCE(SUM(type='sent' AND kind IN ('reminder','reminder2')),0) AS reminders,
      COALESCE(SUM(type='failed'),0) AS failed,
      COALESCE(SUM(type='bounced_hard'),0) AS hard,
      COALESCE(SUM(type='bounced_soft'),0) AS soft,
      COUNT(DISTINCT CASE WHEN type='click' AND kind='human' THEN COALESCE(rid, id) END) AS clicks,
      COALESCE(SUM(type='started'),0) AS started,
      COALESCE(SUM(type='completed'),0) AS completed,
      COALESCE(SUM(type='consent_declined'),0) AS declined,
      COALESCE(SUM(type='opted_out'),0) AS opted_out,
      COALESCE(SUM(type='complaint'),0) AS complaints,
      COALESCE(SUM(type='reply'),0) AS replies
    FROM events WHERE ts >= ? AND ts < ? AND test = 0`).bind(start, end).first<Omit<DayStats, "day">>();
  const s: DayStats = { day, ...(r as Omit<DayStats, "day">) };
  await db.prepare(`INSERT INTO daily_stats(day, invites, reminders, failed, hard, soft, clicks, started, completed, declined, opted_out, complaints, replies, updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(day) DO UPDATE SET invites=excluded.invites, reminders=excluded.reminders, failed=excluded.failed, hard=excluded.hard,
        soft=excluded.soft, clicks=excluded.clicks, started=excluded.started, completed=excluded.completed, declined=excluded.declined,
        opted_out=excluded.opted_out, complaints=excluded.complaints, replies=excluded.replies, updated_at=excluded.updated_at`)
    .bind(day, s.invites, s.reminders, s.failed, s.hard, s.soft, s.clicks, s.started, s.completed, s.declined, s.opted_out, s.complaints, s.replies, nowIso()).run();
  return s;
}

export function todayIst(): string {
  return istParts(new Date()).date;
}

/** Headline KPIs over all (non-test) contacts. */
export async function kpis(db: D1Database): Promise<Record<string, number>> {
  const c = await db.prepare(`SELECT
      COUNT(*) AS contacts,
      COALESCE(SUM(status NOT IN ('on_hold','do_not_contact')),0) AS sendable,
      COALESCE(SUM(status = 'on_hold'),0) AS on_hold,
      COALESCE(SUM(status = 'queued'),0) AS queued,
      COALESCE(SUM(invited_at IS NOT NULL),0) AS invited,
      COALESCE(SUM(reminder_count > 0),0) AS reminded,
      COALESCE(SUM(reminder_count > 1),0) AS reminded2,
      COALESCE(SUM(bounced = 'hard'),0) AS bounced_hard,
      COALESCE(SUM(bounced = 'soft'),0) AS bounced_soft,
      COALESCE(SUM(first_click_at IS NOT NULL),0) AS clicked_any,
      COALESCE(SUM(started_at IS NOT NULL),0) AS started,
      COALESCE(SUM(completed_at IS NOT NULL),0) AS completed,
      COALESCE(SUM(consent_declined_at IS NOT NULL),0) AS declined,
      COALESCE(SUM(opted_out_at IS NOT NULL),0) AS opted_out,
      COALESCE(SUM(complained_at IS NOT NULL),0) AS complaints,
      COALESCE(SUM(status = 'do_not_contact'),0) AS do_not_contact
    FROM contacts WHERE is_test = 0`).first<Record<string, number>>();
  const e = await db.prepare(`SELECT
      COALESCE(SUM(type='sent'),0) AS sent,
      COALESCE(SUM(type='sent' AND kind='invite'),0) AS sent_invites,
      COALESCE(SUM(type='sent' AND kind IN ('reminder','reminder2')),0) AS sent_reminders,
      COALESCE(SUM(type='failed'),0) AS failed
    FROM events WHERE test = 0 AND type IN ('sent','failed')`).first<Record<string, number>>();
  const human = await db.prepare(`SELECT COUNT(DISTINCT rid) AS n FROM events WHERE type='click' AND kind='human' AND rid IS NOT NULL AND test = 0`).first<{ n: number }>();
  const organic = await db.prepare(`SELECT COALESCE(SUM(type='started'),0) AS started, COALESCE(SUM(type='completed'),0) AS completed
    FROM events WHERE kind='organic' AND type IN ('started','completed')`).first<Record<string, number>>();
  const open = await db.prepare(`SELECT COUNT(*) AS n FROM replies r LEFT JOIN contacts c ON c.rid = r.rid WHERE r.handled = 0 AND COALESCE(c.is_test,0) = 0`).first<{ n: number }>();
  const k = { ...(c || {}), ...(e || {}) } as Record<string, number>;
  const invited = k.invited || 0;
  const rate = (n: number) => (invited ? n / invited : 0);
  return {
    ...k,
    delivered: Math.max(0, (k.sent || 0) - (k.bounced_hard || 0)),
    clicked_human: human?.n || 0,
    hard_bounce_rate: rate(k.bounced_hard || 0),
    soft_bounce_rate: rate(k.bounced_soft || 0),
    ctr: rate(human?.n || 0),
    ctr_any: rate(k.clicked_any || 0),
    completion_rate: rate(k.completed || 0),
    organic_started: organic?.started || 0,
    organic_completed: organic?.completed || 0,
    replies_open: open?.n || 0,
  };
}
