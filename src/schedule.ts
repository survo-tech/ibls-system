/**
 * Pure scheduling helpers (no Cloudflare APIs, no imports) so they can be unit-tested with
 * `node --test` directly. All calendar logic runs in Asia/Kolkata (IST, UTC+05:30, no DST).
 */

export const IST_OFFSET_MIN = 330;

/**
 * Canonical ISO-8601 UTC string ("YYYY-MM-DDTHH:MM:SS.sssZ") built from UTC getters.
 * Used instead of Date#toISOString so behaviour never depends on a patched runtime.
 */
export function isoOf(d: Date): string {
  const p = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${p(d.getUTCFullYear(), 4)}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}T${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}.${p(d.getUTCMilliseconds(), 3)}Z`;
}

export interface ScheduleConfig {
  startDate: string;            // YYYY-MM-DD (IST) first day sending may happen
  endDate: string;              // YYYY-MM-DD (IST) last day sending may happen ("" = none)
  windowStart: number;          // minutes after IST midnight, e.g. 600 = 10:00
  windowEnd: number;            // minutes after IST midnight, e.g. 1020 = 17:00
  weekdays: number[];           // ISO weekdays allowed, 1 = Mon … 7 = Sun
  blackout: Set<string>;        // YYYY-MM-DD dates with no sending
  warmupCaps: number[];         // per-mailbox daily cap at warm-up stage 1, 2, 3, … (last value repeats)
  capMax: number;               // ceiling on the per-mailbox daily cap (never above DAILY_HARD_MAX)
  hourlyCap: number;            // per-mailbox cap over any rolling 60 minutes
  reminderAfterWorkdays: number;
  reminder2Enabled: boolean;
  reminder2AfterWorkdays: number;
}

/** "HH:MM" -> minutes after midnight. */
export function parseHHMM(s: string, fallback: number): number {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(s || "").trim());
  if (!m) return fallback;
  const v = Number(m[1]) * 60 + Number(m[2]);
  return v >= 0 && v <= 24 * 60 ? v : fallback;
}

/** Parse "2026-11-05..2026-11-11, 2026-12-25" (commas, spaces or newlines) into a set of dates. */
export function parseBlackout(s: string): Set<string> {
  const out = new Set<string>();
  for (const part of String(s || "").split(/[\s,;]+/).filter(Boolean)) {
    const range = /^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/.exec(part);
    if (range) {
      let d = range[1];
      let guard = 0;
      while (d <= range[2] && guard++ < 400) { out.add(d); d = addDays(d, 1); }
    } else if (/^\d{4}-\d{2}-\d{2}$/.test(part)) {
      out.add(part);
    }
  }
  return out;
}

export function parseRamp(s: string): number[] {
  const v = String(s || "").split(/[\s,]+/).map(Number).filter((n) => Number.isFinite(n) && n >= 0);
  return v.length ? v : [300];
}

/** Calendar date arithmetic on YYYY-MM-DD strings (UTC-based, so no DST surprises). */
export function addDays(date: string, n: number): string {
  const t = Date.UTC(+date.slice(0, 4), +date.slice(5, 7) - 1, +date.slice(8, 10)) + n * 86400000;
  return isoOf(new Date(t)).slice(0, 10);
}

/** ISO weekday (1 = Mon … 7 = Sun) of a YYYY-MM-DD date. */
export function isoWeekday(date: string): number {
  const d = new Date(Date.UTC(+date.slice(0, 4), +date.slice(5, 7) - 1, +date.slice(8, 10))).getUTCDay();
  return d === 0 ? 7 : d;
}

/** IST view of an instant. */
export function istParts(now: Date): { date: string; minutes: number; weekday: number } {
  const shifted = new Date(now.getTime() + IST_OFFSET_MIN * 60000);
  const date = isoOf(shifted).slice(0, 10);
  return { date, minutes: shifted.getUTCHours() * 60 + shifted.getUTCMinutes(), weekday: isoWeekday(date) };
}

/** UTC ISO instant of IST midnight at the start of `date`. */
export function istMidnightUtc(date: string): string {
  const t = Date.UTC(+date.slice(0, 4), +date.slice(5, 7) - 1, +date.slice(8, 10)) - IST_OFFSET_MIN * 60000;
  return isoOf(new Date(t));
}

/** IST calendar date of a stored UTC ISO timestamp. */
export function istDateOf(iso: string): string {
  return istParts(new Date(iso)).date;
}

/** A "workday" = allowed weekday that is not a blackout date (start/end dates are NOT considered). */
export function isWorkday(date: string, cfg: Pick<ScheduleConfig, "weekdays" | "blackout">): boolean {
  return cfg.weekdays.includes(isoWeekday(date)) && !cfg.blackout.has(date);
}

/** A send day = workday within [startDate, endDate]. */
export function isSendDay(date: string, cfg: ScheduleConfig): boolean {
  if (cfg.startDate && date < cfg.startDate) return false;
  if (cfg.endDate && date > cfg.endDate) return false;
  return isWorkday(date, cfg);
}

/** The date that is `n` workdays before `date` (n = 0 returns date). */
export function workdaysBefore(date: string, n: number, cfg: Pick<ScheduleConfig, "weekdays" | "blackout">): string {
  let d = date;
  let left = n;
  let guard = 0;
  while (left > 0 && guard++ < 1000) {
    d = addDays(d, -1);
    if (isWorkday(d, cfg)) left--;
  }
  return d;
}

/**
 * Latest invite date (IST) that is due for a reminder today: an invite sent on day D gets its
 * reminder on the N-th workday after D. Equivalent: D <= workdaysBefore(today, N).
 */
export function reminderCutoffDate(today: string, n: number, cfg: Pick<ScheduleConfig, "weekdays" | "blackout">): string {
  return workdaysBefore(today, n, cfg);
}

/**
 * Absolute per-mailbox daily ceiling. Google Workspace trial accounts may send 500 messages a day per
 * user; this leaves headroom for researchers' own replies and the daily report. No setting can raise it.
 */
export const DAILY_HARD_MAX = 450;

/** Per-mailbox daily cap at warm-up stage k (k starts at 1; the last value repeats). */
export function rampCap(k: number, ramp: number[], capMax: number): number {
  const v = ramp[Math.min(Math.max(k, 1), ramp.length) - 1];
  return Math.max(0, Math.min(v, capMax, DAILY_HARD_MAX));
}

// ---------------------------------------------------------------------------------------------
// Per-mailbox warm-up
//   stage 0  = seed-test day: the mailbox may only send to the seeded test contacts (test_recipients)
//   stage k  = warmup_caps[k-1] per day (default 20,40,60,100,150,200,250)
// The stage is re-evaluated once per IST day from the mailbox's last weekday with sends.
// ---------------------------------------------------------------------------------------------

export interface WarmupConfig {
  caps: number[];              // stage 1.. caps
  seedCap: number;             // max seed (test) messages per mailbox on the seed-test day
  maxBounceRate: number;       // advance only if the window hard-bounce rate is below this (0.02)
  stepbackBounceRate: number;  // step back one stage at or above this rate (0.04)
  windowDays: number;          // the "last window" = the mailbox's last N send days
  capMax: number;              // settings cap_max (clamped to DAILY_HARD_MAX)
}

export function warmupConfigFromSettings(s: Record<string, string>): WarmupConfig {
  return {
    caps: parseRamp(s.warmup_caps || "20,40,60,100,150,200,250").map((n) => Math.min(n, DAILY_HARD_MAX)),
    seedCap: Math.max(0, Math.min(num(s.warmup_seed_cap, 5), 50)),
    maxBounceRate: num(s.warmup_max_bounce_rate, 0.02),
    stepbackBounceRate: num(s.warmup_stepback_bounce_rate, 0.04),
    windowDays: Math.max(1, Math.min(num(s.warmup_window_days, 3), 30)),
    capMax: Math.min(num(s.cap_max, DAILY_HARD_MAX), DAILY_HARD_MAX),
  };
}

/** Highest stage number (stage = caps.length is the plateau). */
export function maxStage(wc: WarmupConfig): number {
  return Math.max(1, wc.caps.length);
}

/** Daily cap for a mailbox at `stage` (stage 0 = seed-test day: only seed messages). */
export function stageCap(stage: number, wc: WarmupConfig): number {
  if (stage <= 0) return Math.max(0, Math.min(wc.seedCap, wc.capMax, DAILY_HARD_MAX));
  return rampCap(Math.min(stage, maxStage(wc)), wc.caps, wc.capMax);
}

export type WarmupAction = "advance" | "hold" | "stepback" | "at_max";

export interface WarmupWindow { sends: number; hard: number; complaints: number }

/**
 * Decide the next stage after a weekday on which the mailbox actually sent.
 *   complaints > 0 or rate >= stepbackBounceRate -> step back one stage (never below stage 1)
 *   rate >= maxBounceRate                        -> hold
 *   otherwise                                    -> advance one stage (or stay at the plateau)
 */
export function decideWarmup(stage: number, w: WarmupWindow, wc: WarmupConfig): { stage: number; action: WarmupAction; rate: number; reason: string } {
  const rate = w.sends > 0 ? w.hard / w.sends : 0;
  const pct = (x: number) => (x * 100).toFixed(1) + "%";
  if (w.sends <= 0) return { stage, action: "hold", rate, reason: "no sends in the window" };
  if (w.complaints > 0) {
    const next = stage >= 2 ? stage - 1 : stage;
    return { stage: next, action: next < stage ? "stepback" : "hold", rate, reason: `${w.complaints} complaint(s) in the last window` };
  }
  if (rate >= wc.stepbackBounceRate) {
    const next = stage >= 2 ? stage - 1 : stage;
    return { stage: next, action: next < stage ? "stepback" : "hold", rate, reason: `hard-bounce rate ${pct(rate)} >= ${pct(wc.stepbackBounceRate)}` };
  }
  if (rate >= wc.maxBounceRate) return { stage, action: "hold", rate, reason: `hard-bounce rate ${pct(rate)} >= ${pct(wc.maxBounceRate)}` };
  if (stage >= maxStage(wc)) return { stage, action: "at_max", rate, reason: `hard-bounce rate ${pct(rate)}, at the top stage` };
  return { stage: stage + 1, action: "advance", rate, reason: `hard-bounce rate ${pct(rate)} < ${pct(wc.maxBounceRate)}, no complaints` };
}

export interface WindowState { open: boolean; minutesInto: number; windowMinutes: number; reason?: string }

/** Is sending allowed right now (send day + inside the daily window)? */
export function windowState(now: Date, cfg: ScheduleConfig): WindowState {
  const p = istParts(now);
  const windowMinutes = Math.max(1, cfg.windowEnd - cfg.windowStart);
  if (cfg.startDate && p.date < cfg.startDate) return { open: false, minutesInto: 0, windowMinutes, reason: `before start_date ${cfg.startDate}` };
  if (cfg.endDate && p.date > cfg.endDate) return { open: false, minutesInto: 0, windowMinutes, reason: `after end_date ${cfg.endDate}` };
  if (cfg.blackout.has(p.date)) return { open: false, minutesInto: 0, windowMinutes, reason: `blackout date ${p.date}` };
  if (!cfg.weekdays.includes(p.weekday)) return { open: false, minutesInto: 0, windowMinutes, reason: "not a send weekday" };
  if (p.minutes < cfg.windowStart || p.minutes >= cfg.windowEnd) return { open: false, minutesInto: 0, windowMinutes, reason: "outside send window" };
  return { open: true, minutesInto: p.minutes - cfg.windowStart, windowMinutes };
}

/**
 * Even pacing: how many sends a mailbox may have made by now today so the daily cap is spread
 * over the window instead of front-loaded. Allows a 30-minute head start so short runs at the
 * start of the window still send something.
 */
export function paceAllowance(dailyCap: number, minutesInto: number, windowMinutes: number): number {
  const frac = Math.min(1, (minutesInto + 30) / windowMinutes);
  return Math.ceil(dailyCap * frac);
}

/** How many new messages a mailbox may take now. */
export function remainingCapacity(args: {
  dailyCap: number; sentToday: number; sentLastHour: number; reservedActive: number;
  hourlyCap: number; perRunMax: number; requested: number; pace?: { minutesInto: number; windowMinutes: number } | null;
}): number {
  const used = args.sentToday + args.reservedActive;
  let room = Math.min(args.dailyCap - used, args.hourlyCap - (args.sentLastHour + args.reservedActive), args.perRunMax, args.requested);
  if (args.pace) room = Math.min(room, paceAllowance(args.dailyCap, args.pace.minutesInto, args.pace.windowMinutes) - used);
  return Math.max(0, Math.floor(room));
}

export interface MailboxProjectionInput {
  mailbox: string;
  sendDaysUsed: number;          // distinct IST days this mailbox already sent on (before today)
  stage?: number;                // current warm-up stage (if given, used instead of sendDaysUsed)
  sentToday: number;             // sends already made today
  queued: number;                // invites not yet sent
  pendingReminders: Record<string, number>;   // invite IST date -> contacts still awaiting reminder 1
  pendingReminders2?: Record<string, number>; // reminder-1 IST date -> contacts awaiting reminder 2
}

export interface ProjectionResult {
  lastInviteDate: string | null;
  finishDate: string | null;     // last day with any planned send (invites or reminders)
  sendDays: number;
  days: { date: string; invites: number; reminders: number }[];  // combined across mailboxes (first 120 days)
  truncated: boolean;
}

/**
 * Day-by-day simulation of the remaining work at the configured caps.
 * `remindRate` = share of invitees still needing a reminder (not completed/bounced/opted out).
 */
export function projectSchedule(now: Date, cfg: ScheduleConfig, boxes: MailboxProjectionInput[], remindRate = 0.9, sharedQueued?: number, maxDays = 120): ProjectionResult {
  const p = istParts(now);
  // With a shared invite queue (sharedQueued given), invites are drawn from one pool by whichever mailbox has room.
  let pool = sharedQueued ?? 0;
  const shared = sharedQueued !== undefined;
  const state = boxes.map((b) => ({
    k: b.stage !== undefined ? b.stage - 1 : b.sendDaysUsed,
    queued: b.queued,
    rem1: { ...b.pendingReminders } as Record<string, number>,
    rem2: { ...(b.pendingReminders2 || {}) } as Record<string, number>,
    sentToday: b.sentToday,
  }));
  const daysOut: ProjectionResult["days"] = [];
  let lastInvite: string | null = null;
  let finish: string | null = null;
  let sendDays = 0;
  let d = p.date;
  // Today only counts if the window has not closed yet.
  const todayUsable = p.minutes < cfg.windowEnd;
  if (!todayUsable) d = addDays(d, 1);
  const pendingTotal = () => (shared ? pool : 0) + state.reduce((a, s) => a + (shared ? 0 : s.queued) + sum(s.rem1) + (cfg.reminder2Enabled ? sum(s.rem2) : 0), 0);
  let guard = 0;
  while (pendingTotal() > 0.5 && guard++ < 730) {
    if (isSendDay(d, cfg)) {
      let dayInv = 0, dayRem = 0;
      const cut1 = reminderCutoffDate(d, cfg.reminderAfterWorkdays, cfg);
      const cut2 = reminderCutoffDate(d, cfg.reminder2AfterWorkdays, cfg);
      for (const s of state) {
        const isToday = d === p.date;
        const k = s.k + 1; // sendDaysUsed counts days before today
        // k <= 0 is the seed-test day: no real sends, the mailbox moves to stage 1 afterwards.
        const cap = k <= 0 ? 0 : Math.max(0, rampCap(k, cfg.warmupCaps, cfg.capMax) - (isToday ? s.sentToday : 0));
        let used = 0;
        // reminders first
        for (const [bucket, cut, next] of [[s.rem1, cut1, true], [s.rem2, cut2, false]] as [Record<string, number>, string, boolean][]) {
          if (bucket === s.rem2 && !cfg.reminder2Enabled) continue;
          for (const date of Object.keys(bucket).sort()) {
            if (date > cut || cap - used <= 0) continue;
            const take = Math.min(bucket[date], cap - used);
            bucket[date] -= take; if (bucket[date] <= 0.0001) delete bucket[date];
            used += take; dayRem += take;
            if (next && cfg.reminder2Enabled) s.rem2[d] = (s.rem2[d] || 0) + take * remindRate;
          }
        }
        const inv = Math.min(shared ? pool : s.queued, Math.max(0, cap - used));
        if (inv > 0) {
          if (shared) pool -= inv; else s.queued -= inv;
          s.rem1[d] = (s.rem1[d] || 0) + inv * remindRate; used += inv; dayInv += inv;
        }
        if (used > 0 || isToday || k <= 0) s.k = k;
      }
      if (dayInv + dayRem > 0) {
        sendDays++;
        finish = d;
        if (dayInv > 0) lastInvite = d;
        if (daysOut.length < maxDays) daysOut.push({ date: d, invites: Math.round(dayInv), reminders: Math.round(dayRem) });
      }
    }
    if (cfg.endDate && d > cfg.endDate) break;
    d = addDays(d, 1);
  }
  return { lastInviteDate: lastInvite, finishDate: finish, sendDays, days: daysOut, truncated: pendingTotal() > 0.5 };
}

function sum(o: Record<string, number>): number {
  let t = 0; for (const v of Object.values(o)) t += v; return t;
}

/** Build a ScheduleConfig from the flat key/value settings map. */
export function configFromSettings(s: Record<string, string>): ScheduleConfig {
  return {
    startDate: s.start_date || "",
    endDate: s.end_date || "",
    windowStart: parseHHMM(s.window_start, 600),
    windowEnd: parseHHMM(s.window_end, 1020),
    weekdays: String(s.send_weekdays || "1,2,3,4,5").split(/[\s,]+/).map(Number).filter((n) => n >= 1 && n <= 7),
    blackout: parseBlackout(s.blackout_dates || ""),
    warmupCaps: parseRamp(s.warmup_caps || "20,40,60,100,150,200,250"),
    capMax: Math.min(num(s.cap_max, DAILY_HARD_MAX), DAILY_HARD_MAX),
    hourlyCap: num(s.hourly_cap, 120),
    reminderAfterWorkdays: num(s.reminder_after_workdays, 5),
    reminder2Enabled: s.reminder2_enabled === "true",
    reminder2AfterWorkdays: num(s.reminder2_after_workdays, 5),
  };
}

function num(v: string | undefined, d: number): number {
  const n = Number(v);
  return v !== undefined && v !== "" && Number.isFinite(n) ? n : d;
}

/** Stratified interleave key, documented here for reference; the import script computes it in Python. */
export const SEND_ORDER_DOC = "send_order = rank of (i + u)/n within each region×size_band×nature×email_tier stratum";

/** The date that is `n` workdays after `date` (n = 0 returns date). */
export function workdaysAfter(date: string, n: number, cfg: Pick<ScheduleConfig, "weekdays" | "blackout">): string {
  let d = date, left = n, guard = 0;
  while (left > 0 && guard++ < 400) { d = addDays(d, 1); if (isWorkday(d, cfg)) left--; }
  return d;
}

/**
 * Spread `count` queued contacts (already sorted by send_order) over the projected days' invite totals.
 * Returns one projected IST date per contact, or "" once the projection runs out.
 */
export function assignInviteDates(days: { date: string; invites: number }[], count: number): string[] {
  const out: string[] = [];
  for (const day of days) {
    for (let i = 0; i < day.invites && out.length < count; i++) out.push(day.date);
    if (out.length >= count) break;
  }
  while (out.length < count) out.push("");
  return out;
}
