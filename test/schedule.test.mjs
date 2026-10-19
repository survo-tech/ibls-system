// Unit tests for the pure scheduling helpers. Run: node --test test/  (Node >= 22.18 strips TS types natively)
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  addDays, configFromSettings, DAILY_HARD_MAX, decideWarmup, isoOf, istMidnightUtc, istParts, paceAllowance, parseBlackout, projectSchedule,
  rampCap, reminderCutoffDate, remainingCapacity, stageCap, warmupConfigFromSettings, windowState, workdaysBefore,
} from "../src/schedule.ts";

const S = {
  start_date: "2026-10-27", end_date: "", window_start: "10:00", window_end: "17:00", send_weekdays: "1,2,3,4,5",
  blackout_dates: "2026-11-05..2026-11-11", warmup_caps: "20,40,60,100,150,200,250", cap_max: "450", hourly_cap: "120",
  reminder_after_workdays: "5", reminder2_enabled: "false", reminder2_after_workdays: "5",
};
const cfg = configFromSettings(S);

test("isoOf is canonical UTC", () => {
  assert.equal(isoOf(new Date(Date.UTC(2026, 9, 27, 4, 30, 5, 7))), "2026-10-27T04:30:05.007Z");
});

test("blackout ranges expand inclusively", () => {
  const b = parseBlackout("2026-11-05..2026-11-11, 2026-12-25");
  assert.equal(b.size, 8);
  assert.ok(b.has("2026-11-05") && b.has("2026-11-11") && b.has("2026-12-25") && !b.has("2026-11-12"));
});

test("IST conversion", () => {
  const p = istParts(new Date("2026-10-27T04:30:00Z"));
  assert.deepEqual(p, { date: "2026-10-27", minutes: 600, weekday: 2 });
  assert.equal(istMidnightUtc("2026-10-27"), "2026-10-26T18:30:00.000Z");
  assert.equal(addDays("2026-10-31", 1), "2026-11-01");
});

test("send window: open 10:00-17:00 IST on workdays from start date", () => {
  assert.equal(windowState(new Date("2026-10-27T04:29:00Z"), cfg).open, false); // 09:59 IST
  assert.equal(windowState(new Date("2026-10-27T04:30:00Z"), cfg).open, true);  // 10:00 IST
  assert.equal(windowState(new Date("2026-10-27T11:29:00Z"), cfg).open, true);  // 16:59 IST
  assert.equal(windowState(new Date("2026-10-27T11:30:00Z"), cfg).open, false); // 17:00 IST
  assert.match(windowState(new Date("2026-10-26T06:00:00Z"), cfg).reason, /before start_date/);
  assert.match(windowState(new Date("2026-10-31T06:00:00Z"), cfg).reason, /weekday/);       // Saturday
  assert.match(windowState(new Date("2026-11-06T06:00:00Z"), cfg).reason, /blackout/);      // Diwali week
});

test("reminder cutoff skips weekends and Diwali blackout", () => {
  // Invite Tue 27 Oct -> 5 workdays later = Tue 3 Nov.
  assert.equal(reminderCutoffDate("2026-11-03", 5, cfg), "2026-10-27");
  // Invite Wed 4 Nov -> workdays after: 12,13,16,17,18 Nov -> reminder Wed 18 Nov.
  assert.equal(workdaysBefore("2026-11-18", 5, cfg), "2026-11-04");
  assert.equal(workdaysBefore("2026-11-17", 5, cfg), "2026-11-03");
});

test("warm-up caps per stage, never above 450/day", () => {
  const wc = warmupConfigFromSettings(S);
  assert.deepEqual([0, 1, 2, 3, 4, 5, 6, 7, 30].map((k) => stageCap(k, wc)), [5, 20, 40, 60, 100, 150, 200, 250, 250]);
  assert.equal(DAILY_HARD_MAX, 450);
  // Even a mis-typed config cannot exceed the hard limit.
  const big = warmupConfigFromSettings({ ...S, warmup_caps: "20,400,900", cap_max: "2000" });
  assert.deepEqual([1, 2, 3].map((k) => stageCap(k, big)), [20, 400, 450]);
  assert.equal(rampCap(3, [20, 400, 900], 9999), 450);
  assert.equal(stageCap(7, warmupConfigFromSettings({ ...S, cap_max: "120" })), 120);
});

test("warm-up decisions: advance, hold, step back", () => {
  const wc = warmupConfigFromSettings(S);
  // seed day with clean seed sends -> stage 1
  assert.deepEqual(pick(decideWarmup(0, { sends: 5, hard: 0, complaints: 0 }, wc)), [1, "advance"]);
  // clean day at stage 1 -> stage 2
  assert.deepEqual(pick(decideWarmup(1, { sends: 20, hard: 0, complaints: 0 }, wc)), [2, "advance"]);
  // 1 hard bounce in 60 sends = 1.7% (< 2%) -> advance
  assert.deepEqual(pick(decideWarmup(3, { sends: 60, hard: 1, complaints: 0 }, wc)), [4, "advance"]);
  // exactly 2% -> hold
  assert.deepEqual(pick(decideWarmup(4, { sends: 100, hard: 2, complaints: 0 }, wc)), [4, "hold"]);
  // 3% -> hold (below the 4% step-back rate)
  assert.deepEqual(pick(decideWarmup(4, { sends: 100, hard: 3, complaints: 0 }, wc)), [4, "hold"]);
  // 5% -> step back one stage
  assert.deepEqual(pick(decideWarmup(5, { sends: 100, hard: 5, complaints: 0 }, wc)), [4, "stepback"]);
  // any complaint -> step back, even with no bounces
  assert.deepEqual(pick(decideWarmup(6, { sends: 400, hard: 0, complaints: 1 }, wc)), [5, "stepback"]);
  // never below stage 1 once warm-up has started
  assert.deepEqual(pick(decideWarmup(1, { sends: 20, hard: 5, complaints: 0 }, wc)), [1, "hold"]);
  // plateau
  assert.deepEqual(pick(decideWarmup(7, { sends: 250, hard: 0, complaints: 0 }, wc)), [7, "at_max"]);
  assert.deepEqual(pick(decideWarmup(3, { sends: 0, hard: 0, complaints: 0 }, wc)), [3, "hold"]);
});

function pick(d) { return [d.stage, d.action]; }

test("capacity honours daily, hourly, pacing, per-run and request limits", () => {
  const base = { dailyCap: 300, sentToday: 0, sentLastHour: 0, reservedActive: 0, hourlyCap: 120, perRunMax: 20, requested: 50, pace: null };
  assert.equal(remainingCapacity(base), 20);
  assert.equal(remainingCapacity({ ...base, sentToday: 295 }), 5);
  assert.equal(remainingCapacity({ ...base, sentLastHour: 115 }), 5);
  assert.equal(remainingCapacity({ ...base, sentLastHour: 110, reservedActive: 10 }), 0);
  assert.equal(remainingCapacity({ ...base, requested: 3 }), 3);
  // Pacing: 0 min into a 420-min window, cap 40 -> ceil(40*30/420) = 3 allowed so far.
  assert.equal(paceAllowance(40, 0, 420), 3);
  assert.equal(remainingCapacity({ ...base, dailyCap: 40, pace: { minutesInto: 0, windowMinutes: 420 } }), 3);
  assert.equal(remainingCapacity({ ...base, dailyCap: 40, sentToday: 40, pace: { minutesInto: 400, windowMinutes: 420 } }), 0);
});

test("projection finishes and respects blackout + warm-up (legacy per-mailbox queues)", () => {
  const boxes = [
    { mailbox: "a", sendDaysUsed: 0, sentToday: 0, queued: 3985, pendingReminders: {} },
    { mailbox: "b", sendDaysUsed: 0, sentToday: 0, queued: 3984, pendingReminders: {} },
  ];
  const p = projectSchedule(new Date("2026-10-20T00:00:00Z"), cfg, boxes, 0.9);
  assert.equal(p.truncated, false);
  assert.equal(p.days[0].date, "2026-10-27");
  assert.equal(p.days[0].invites, 40); // 20 per mailbox at stage 1
  assert.equal(p.days[1].invites, 80);
  assert.ok(!p.days.some((d) => d.date >= "2026-11-05" && d.date <= "2026-11-11"), "no sends during blackout");
  assert.ok(p.finishDate > p.lastInviteDate);
});

test("projection: five mailboxes, shared queue, seed day first, never above 5 x 450", () => {
  const five = ["participate", "research", "survey", "study", "team"].map((m) => ({ mailbox: m, sendDaysUsed: 0, sentToday: 0, queued: 0, pendingReminders: {}, stage: 0 }));
  const p = projectSchedule(new Date("2026-10-20T00:00:00Z"), cfg, five, 0.9, 7969);
  assert.equal(p.truncated, false);
  // 27 Oct is the seed-test day (no real sends), so real invites start on 28 Oct at 5 x 20.
  assert.equal(p.days[0].date, "2026-10-28");
  assert.equal(p.days[0].invites, 100);
  assert.equal(p.days[1].invites, 200);
  assert.equal(p.days[2].invites, 300);
  assert.ok(p.days.every((d) => d.invites + d.reminders <= 5 * 250));
  assert.ok(!p.days.some((d) => d.date >= "2026-11-05" && d.date <= "2026-11-11"));
  const firstRem = p.days.find((d) => d.reminders > 0);
  assert.equal(firstRem.date, "2026-11-04"); // 5 workdays after 28 Oct
  assert.ok(p.finishDate <= "2026-12-18", p.finishDate);
  // mailboxes already at stage 3 start at 60/day each
  const warm = five.map((b) => ({ ...b, stage: 3 }));
  assert.equal(projectSchedule(new Date("2026-10-20T00:00:00Z"), cfg, warm, 0.9, 7969).days[0].invites, 300);
});
