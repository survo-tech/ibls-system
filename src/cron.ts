/** Every-15-minute housekeeping: stale reservations, stop rules, daily rollups, pruning. */
import { getSettings, logJobRun, pauseSending, setState } from "./db";
import { addDays, configFromSettings, istMidnightUtc, istParts, reminderCutoffDate, windowState, isoOf } from "./schedule";
import { rollupDay } from "./stats";
import { Env, nowIso } from "./util";

export interface StopRuleResult { pause: boolean; reason?: string; checks: Record<string, unknown> }

/** Evaluate auto-pause rules. Exported for the dashboard health panel. */
export async function evaluateStopRules(db: D1Database, s: Record<string, string>, now = new Date()): Promise<StopRuleResult> {
  const checks: Record<string, unknown> = {};
  const resetAt = s.stop_rule_reset_at || "1970-01-01T00:00:00.000Z";

  // 1. Spam complaints: >= threshold in total AND at least one new since the last resume.
  const comp = await db.prepare("SELECT COUNT(*) AS n FROM contacts WHERE complained_at IS NOT NULL AND is_test = 0").first<{ n: number }>();
  const complaints = comp?.n || 0;
  const threshold = Number(s.complaint_threshold) || 3;
  const baseline = Number(s.stop_rule_complaint_baseline) || 0;
  checks.complaints = { total: complaints, threshold, baseline };
  if (complaints >= threshold && complaints > baseline) {
    return { pause: true, reason: `Auto-pause: ${complaints} spam complaints (threshold ${threshold}). Review before resuming.`, checks };
  }

  // 2. Hard-bounce rate over the last N attempted sends since the last resume.
  const win = Number(s.bounce_window) || 200;
  const minSample = Number(s.bounce_min_sample) || 50;
  const br = await db.prepare(`WITH recent AS (
        SELECT rid FROM events WHERE test = 0 AND ts > ?1 AND (type = 'sent' OR (type = 'bounced_hard' AND kind = 'smtp'))
        ORDER BY ts DESC LIMIT ?2)
      SELECT (SELECT COUNT(*) FROM recent) AS sends,
             (SELECT COUNT(DISTINCT rid) FROM events WHERE type = 'bounced_hard' AND rid IN (SELECT rid FROM recent)) AS hard`)
    .bind(resetAt, win).first<{ sends: number; hard: number }>();
  const sends = br?.sends || 0, hard = br?.hard || 0;
  const rate = sends ? hard / sends : 0;
  const maxRate = Number(s.bounce_rate_threshold) || 0.03;
  checks.bounce = { sends, hard, rate, threshold: maxRate, min_sample: minSample };
  if (sends >= minSample && rate > maxRate) {
    return { pause: true, reason: `Auto-pause: hard-bounce rate ${(rate * 100).toFixed(1)}% over last ${sends} sends (limit ${(maxRate * 100).toFixed(1)}%).`, checks };
  }

  // 3. Sender heartbeat missing while the window is open and work is due (live mode only).
  const cfg = configFromSettings(s);
  const w = windowState(now, cfg);
  const hbMax = Number(s.heartbeat_max_minutes) || 90;
  const last = await db.prepare("SELECT MAX(ts) AS ts FROM job_runs WHERE job = 'send'").first<{ ts: string | null }>();
  checks.heartbeat = { last_send_run: last?.ts || null, max_minutes: hbMax, window_open: w.open };
  if (s.test_mode !== "true" && w.open && w.minutesInto >= hbMax) {
    const lastMs = last?.ts ? Date.parse(last.ts) : 0;
    if (now.getTime() - lastMs > hbMax * 60000) {
      const today = istParts(now).date;
      const cut = istMidnightUtc(addDays(reminderCutoffDate(today, cfg.reminderAfterWorkdays, cfg), 1));
      const due = await db.prepare(`SELECT
          (SELECT COUNT(*) FROM contacts WHERE status = 'queued' AND is_test = 0) +
          (SELECT COUNT(*) FROM contacts WHERE status = 'invited' AND reminder_count = 0 AND invited_at < ? AND completed_at IS NULL AND is_test = 0) AS n`)
        .bind(cut).first<{ n: number }>();
      if ((due?.n || 0) > 0) {
        return { pause: true, reason: `Auto-pause: no sender heartbeat for over ${hbMax} min during the send window (last: ${last?.ts || "never"}). Check GitHub Actions.`, checks };
      }
    }
  }
  return { pause: false, checks };
}

export async function runCron(env: Env): Promise<Record<string, unknown>> {
  const db = env.DB;
  const out: Record<string, unknown> = { at: nowIso() };
  try {
    // Release reservations whose 30-minute lease expired.
    const rel = await db.prepare("UPDATE contacts SET reserved_until = NULL, reserved_run = NULL, reserved_kind = NULL WHERE reserved_until IS NOT NULL AND reserved_until < ?")
      .bind(nowIso()).run();
    out.released = rel.meta.changes ?? 0;

    const s = await getSettings(db);
    if (s.paused !== "true") {
      const r = await evaluateStopRules(db, s);
      out.stop_rules = r.checks;
      if (r.pause && r.reason) {
        await pauseSending(db, r.reason, "auto:stop-rule");
        await logJobRun(db, "stop_rule", false, r.reason);
        out.paused = r.reason;
      }
    } else {
      out.paused = s.pause_reason || "paused";
    }

    // Trigger the GitHub send workflow ourselves: GitHub's own cron fires hours late on this repo.
    // The workflow's concurrency group keeps at most one run going and one queued.
    if (env.GH_DISPATCH_TOKEN && s.paused !== "true" && out.paused === undefined) {
      const w = windowState(new Date(), configFromSettings(s));
      if (w.open) out.dispatch = await dispatchSend(env);
    }

    // Rollups for today and yesterday (late bounces / Kobo syncs land after midnight).
    const today = istParts(new Date()).date;
    await rollupDay(db, today);
    await rollupDay(db, addDays(today, -1));

    // Keep job_runs small.
    await db.prepare("DELETE FROM job_runs WHERE ts < ?").bind(isoOf(new Date(Date.now() - 60 * 86400000))).run();
    out.ok = true;
  } catch (e) {
    out.ok = false;
    out.error = String((e as Error).message || e);
  }
  await setState(db, { cron_last: JSON.stringify(out) }).catch(() => undefined);
  return out;
}

/** POST a workflow_dispatch for send.yml. Returns the HTTP status (204 = queued). */
export async function dispatchSend(env: Env): Promise<number | string> {
  const repo = env.GH_REPO || "survo-tech/ibls-system";
  try {
    const r = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/send.yml/dispatches`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        accept: "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
        "user-agent": "ibls-survo-worker",
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref: "main" }),
    });
    return r.status;
  } catch (e) {
    return String((e as Error).message || e);
  }
}
