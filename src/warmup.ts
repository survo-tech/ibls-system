/**
 * Per-mailbox warm-up state (persisted as state `warmup:<mailbox>` in the settings table).
 *
 * Stage 0 is the seed-test day (only seeded test contacts), then warmup_caps per day. Once per IST day the
 * stage is re-evaluated from the mailbox's most recent WEEKDAY with sends that has not been evaluated yet:
 * the mailbox advances only if its hard-bounce rate over its last `warmup_window_days` send days is below
 * `warmup_max_bounce_rate` and it drew no complaints; otherwise it holds, or steps back one stage. Days
 * without sends (weekends, blackout, pause, an empty queue) never advance a mailbox.
 */
import { getState, setState } from "./db";
import {
  addDays, decideWarmup, istMidnightUtc, istParts, isoOf, isoWeekday, maxStage, stageCap, WarmupAction, warmupConfigFromSettings,
} from "./schedule";

export interface WarmupDecision {
  day: string; action: WarmupAction | "manual"; from: number; to: number; sends?: number; hard?: number; complaints?: number;
  rate?: number; window_from?: string; reason: string; at: string;
}

export interface WarmupState {
  stage: number;
  evaluated_through: string;   // last IST send day already used for a decision
  last?: WarmupDecision;
  history?: WarmupDecision[];  // newest first, capped
}

export interface WarmupView {
  mailbox: string; stage: number; max_stage: number; stage_label: string; daily_cap: number; sent_today: number;
  remaining_today: number; evaluated_through: string; last: WarmupDecision | null; history: WarmupDecision[];
}

const HISTORY_MAX = 15;
const key = (mb: string) => "warmup:" + mb;

export function parseWarmupState(raw: string | undefined): WarmupState {
  try {
    const v = raw ? JSON.parse(raw) : null;
    if (v && typeof v === "object" && Number.isInteger(v.stage)) {
      return { stage: Math.max(0, v.stage), evaluated_through: String(v.evaluated_through || ""), last: v.last, history: Array.isArray(v.history) ? v.history : [] };
    }
  } catch { /* fall through to the default */ }
  return { stage: 0, evaluated_through: "", history: [] };
}

/**
 * Bring a mailbox's warm-up state up to date for `now` and return it.
 * persist=false computes the same answer without writing (used by read-only views).
 */
export async function evaluateWarmup(db: D1Database, s: Record<string, string>, mailbox: string, now: Date, persist = true): Promise<WarmupState> {
  const wc = warmupConfigFromSettings(s);
  const st = parseWarmupState((await getState(db, [key(mailbox)]))[key(mailbox)]);
  st.stage = Math.min(st.stage, maxStage(wc));
  const today = istParts(now).date;
  const testToo = st.stage === 0 ? 1 : 0; // stage 0 advances on seed (test) sends; later stages only on real sends
  const { results } = await db.prepare(`SELECT DISTINCT substr(datetime(ts, '+330 minutes'), 1, 10) AS d FROM events
      WHERE type = 'sent' AND mailbox = ?1 AND ts < ?2 AND ts >= ?4 AND (?3 = 1 OR test = 0) ORDER BY d DESC LIMIT 40`)
    .bind(mailbox, istMidnightUtc(today), testToo, istMidnightUtc(addDays(today, -60))).all<{ d: string }>();
  const days = results.map((r) => r.d);
  const candidate = days.find((d) => d > st.evaluated_through && isoWeekday(d) <= 5);
  if (!candidate) return st;

  const windowDays = days.filter((d) => d <= candidate).slice(0, wc.windowDays);
  const from = windowDays[windowDays.length - 1];
  const fromUtc = istMidnightUtc(from), toUtc = istMidnightUtc(addDays(candidate, 1));
  // Attempts = sends + send-time hard rejects, as in the global stop rule. Bounces and complaints are counted
  // for the contacts attempted in the window, whenever the DSN / complaint arrived.
  const w = await db.prepare(`WITH att AS (
        SELECT rid FROM events WHERE mailbox = ?1 AND ts >= ?2 AND ts < ?3 AND (?4 = 1 OR test = 0)
          AND (type = 'sent' OR (type = 'bounced_hard' AND kind = 'smtp')))
      SELECT (SELECT COUNT(*) FROM att) AS sends,
        (SELECT COUNT(DISTINCT rid) FROM events WHERE type = 'bounced_hard' AND ts >= ?2 AND rid IN (SELECT rid FROM att WHERE rid IS NOT NULL)) AS hard,
        (SELECT COUNT(DISTINCT COALESCE(rid, id)) FROM events WHERE type = 'complaint' AND ts >= ?2
           AND (rid IN (SELECT rid FROM att WHERE rid IS NOT NULL) OR (rid IS NULL AND mailbox = ?1))) AS complaints`)
    .bind(mailbox, fromUtc, toUtc, testToo).first<{ sends: number; hard: number; complaints: number }>();
  const win = { sends: w?.sends || 0, hard: w?.hard || 0, complaints: w?.complaints || 0 };
  const d = decideWarmup(st.stage, win, wc);
  const decision: WarmupDecision = {
    day: candidate, action: d.action, from: st.stage, to: d.stage, sends: win.sends, hard: win.hard, complaints: win.complaints,
    rate: Math.round(d.rate * 10000) / 10000, window_from: from, reason: d.reason, at: isoOf(now),
  };
  const next: WarmupState = { stage: d.stage, evaluated_through: candidate, last: decision, history: [decision, ...(st.history || [])].slice(0, HISTORY_MAX) };
  if (persist) await setState(db, { [key(mailbox)]: JSON.stringify(next) });
  return next;
}

/** Researcher override from the dashboard: set a mailbox's stage (hold, step back, skip the seed day). */
export async function setWarmupStage(db: D1Database, s: Record<string, string>, mailbox: string, stage: number, actor: string, note: string, now = new Date()): Promise<WarmupState> {
  const wc = warmupConfigFromSettings(s);
  const st = parseWarmupState((await getState(db, [key(mailbox)]))[key(mailbox)]);
  const to = Math.max(0, Math.min(Math.floor(stage), maxStage(wc)));
  const today = istParts(now).date;
  const decision: WarmupDecision = { day: today, action: "manual", from: st.stage, to, reason: `set by ${actor}${note ? ": " + note : ""}`, at: isoOf(now) };
  // Days before today are treated as evaluated, so the override is not immediately overwritten by old sends.
  const next: WarmupState = { stage: to, evaluated_through: addDays(today, -1) > st.evaluated_through ? addDays(today, -1) : st.evaluated_through,
    last: decision, history: [decision, ...(st.history || [])].slice(0, HISTORY_MAX) };
  await setState(db, { [key(mailbox)]: JSON.stringify(next) });
  return next;
}

export function stageLabel(stage: number, cap: number): string {
  return stage <= 0 ? `seed test (max ${cap} seed messages)` : `stage ${stage} (${cap}/day)`;
}

/** Warm-up view for dashboards and the job API. */
export async function warmupView(db: D1Database, s: Record<string, string>, mailbox: string, now: Date, persist = false): Promise<WarmupView> {
  const wc = warmupConfigFromSettings(s);
  const st = await evaluateWarmup(db, s, mailbox, now, persist);
  const cap = stageCap(st.stage, wc);
  const row = await db.prepare(`SELECT COUNT(*) AS n FROM events WHERE mailbox = ? AND ts >= ?
      AND (type = 'sent' OR (type = 'bounced_hard' AND kind = 'smtp'))`).bind(mailbox, istMidnightUtc(istParts(now).date)).first<{ n: number }>();
  const sent = row?.n || 0;
  return {
    mailbox, stage: st.stage, max_stage: maxStage(wc), stage_label: stageLabel(st.stage, cap), daily_cap: cap, sent_today: sent,
    remaining_today: Math.max(0, cap - sent), evaluated_through: st.evaluated_through, last: st.last || null, history: st.history || [],
  };
}
