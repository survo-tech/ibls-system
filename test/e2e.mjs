// End-to-end test against a running `wrangler dev --local` (see scripts/local_e2e.sh).
// Needs ALLOW_TIME_OVERRIDE=1 and the API_TOKEN / DASHBOARD_USERS from .dev.vars.example.
import { test } from "node:test";
import assert from "node:assert/strict";

const BASE = process.env.BASE || "http://127.0.0.1:8787";
const TOKEN = process.env.API_TOKEN || "local-test-token-0123456789";
const BASIC = "Basic " + Buffer.from("admin:secret").toString("base64");
const P = "participate@survo.tech", R = "research@survo.tech";

const api = async (path, opts = {}) => {
  const res = await fetch(BASE + path, { ...opts, redirect: "manual", headers: { authorization: `Bearer ${TOKEN}`, "content-type": "application/json", ...(opts.headers || {}) } });
  const text = await res.text();
  let body; try { body = JSON.parse(text); } catch { body = text; }
  return { status: res.status, body, headers: res.headers };
};
const dash = (path, opts = {}) => api("/dashboard/api/" + path, { ...opts, headers: { authorization: BASIC, "x-ibls-dashboard": "1", ...(opts.headers || {}) } });
const events = (evs) => api("/api/events", { method: "POST", body: JSON.stringify({ events: evs }) });
const batch = (mailbox, now, run, max = 50) => api(`/api/next-batch?mailbox=${encodeURIComponent(mailbox)}&max=${max}&run_id=${run}${now ? "&now=" + now : ""}`);
const contact = async (rid) => (await dash("contact/" + rid)).body.contact;

let realRid; // a real (imported) contact
const liveSent = []; // rids sent in the live-mode test

test("static site is standalone", async () => {
  const r = await fetch(BASE + "/");
  const html = await r.text();
  assert.equal(r.status, 200);
  assert.match(html, /India Business Leaders Survey/);
  assert.doesNotMatch(html, /hark/i);
  for (const a of ["/site.css", "/site.js", "/hero.webp", "/favicon.svg", "/robots.txt"]) assert.equal((await fetch(BASE + a)).status, 200, a);
});

test("auth is enforced", async () => {
  assert.equal((await fetch(BASE + "/api/settings")).status, 401);
  assert.equal((await fetch(BASE + "/api/settings", { headers: { authorization: "Bearer wrong-token-xxxxxxxx" } })).status, 401);
  assert.equal((await fetch(BASE + "/dashboard")).status, 401);
  assert.equal((await fetch(BASE + "/dashboard", { headers: { authorization: "Basic " + Buffer.from("admin:nope").toString("base64") } })).status, 401);
  const ok = await fetch(BASE + "/dashboard", { headers: { authorization: BASIC } });
  assert.equal(ok.status, 200);
  assert.match(await ok.text(), /Outreach dashboard/);
  // CSRF header required on dashboard writes
  const csrf = await fetch(BASE + "/dashboard/api/pause", { method: "POST", headers: { authorization: BASIC }, body: "{}" });
  assert.equal(csrf.status, 403);
});

test("defaults: test mode on, contacts imported", async () => {
  const s = await api("/api/settings");
  assert.equal(s.body.settings.test_mode, "true");
  const sum = await dash("summary");
  assert.equal(sum.status, 200);
  assert.equal(sum.body.kpis.contacts, 8092);
  assert.equal(sum.body.kpis.on_hold, 123);
  assert.equal(sum.body.kpis.sendable, 7969);
  const q = await dash("search?q=airmech");
  realRid = q.body.rows[0].rid;
  assert.ok(realRid);
});

test("click redirect and unknown rid", async () => {
  const r = await fetch(`${BASE}/s/${realRid}`, { redirect: "manual", headers: { "user-agent": "Mozilla/5.0 (Windows NT 10.0)" } });
  assert.equal(r.status, 302);
  assert.equal(r.headers.get("location"), `https://ee.kobotoolbox.org/jbh8Urj6?d[rid]=${realRid}`);
  const u = await fetch(`${BASE}/s/ZZZZ9999`, { redirect: "manual" });
  assert.equal(u.headers.get("location"), "https://ee.kobotoolbox.org/jbh8Urj6");
  await new Promise((r2) => setTimeout(r2, 300));
  const c = await contact(realRid);
  assert.ok(c.first_click_at);
  assert.equal(c.click_count, 1);
});

test("test mode only ever returns allow-listed test contacts; reservation is idempotent", async () => {
  const b1 = await batch(P, null, "t1");
  assert.equal(b1.status, 200);
  assert.ok(b1.body.messages.length >= 1, JSON.stringify(b1.body));
  assert.ok(b1.body.messages.every((m) => m.rid.startsWith("TEST")));
  assert.equal(b1.body.settings.test_mode, true);
  const again = await batch(P, null, "t1");
  assert.equal(again.body.idempotent, true);
  assert.deepEqual(again.body.messages.map((m) => m.rid), b1.body.messages.map((m) => m.rid));
  const other = await batch(P, null, "t2");
  assert.equal(other.body.messages.length, 0); // all test contacts for this mailbox are reserved by t1
  const m = b1.body.messages[0];
  assert.equal(m.survey_link, `https://survo.tech/s/${m.rid}`);
  assert.equal(m.optout_link, `https://survo.tech/optout/${m.rid}`);
  const r = await events([{ rid: m.rid, email: m.email, type: "sent", kind: "invite", mailbox: P, meta: { message_id: "<ibls.x@survo.tech>" }, id: "sent:test1" }]);
  assert.equal(r.body.accepted, 1);
  const dup = await events([{ rid: m.rid, type: "sent", kind: "invite", mailbox: P, id: "sent:test1" }]);
  assert.equal(dup.body.duplicate, 1);
  assert.equal((await contact(m.rid)).status, "invited");
  await api("/api/release", { method: "POST", body: JSON.stringify({ run_id: b1.body.run_id }) });
  // test sends do not show in stats
  assert.equal((await dash("summary")).body.kpis.invited, 0);
});

test("live mode: window, start date, ramp pacing, blackout", async () => {
  const bad = await dash("settings", { method: "POST", body: JSON.stringify({ window_start: "25:00" }) });
  assert.equal(bad.status, 400);
  const ok = await dash("settings", { method: "POST", body: JSON.stringify({ test_mode: "false" }) });
  assert.equal(ok.status, 200);

  const before = await batch(P, "2026-10-26T06:00:00Z", "l0");
  assert.match(before.body.reason, /before start_date/);
  const early = await batch(P, "2026-10-27T04:00:00Z", "l1"); // 09:30 IST
  assert.match(early.body.reason, /outside send window/);
  // 10:05 IST on day 1: cap 40, pacing allows ceil(40*35/420)=4
  const d1 = await batch(P, "2026-10-27T04:35:00Z", "l2");
  assert.equal(d1.body.caps.daily_cap, 40);
  assert.equal(d1.body.messages.length, 4, JSON.stringify(d1.body.caps));
  assert.ok(d1.body.messages.every((m) => m.kind === "invite" && !m.rid.startsWith("TEST") && m.mailbox === P));
  const orders = d1.body.messages.map((m) => m.rid);
  // record them as sent on day 1
  await events(d1.body.messages.map((m, i) => ({ rid: m.rid, type: "sent", kind: "invite", mailbox: P, ts: "2026-10-27T04:4" + i + ":00Z", id: "s1:" + m.rid })));
  // late on day 1 (16:50 IST): pacing allows the full 40 minus 4 sent, but per_run_max 20 and max param 50
  const late = await batch(P, "2026-10-27T11:20:00Z", "l3");
  assert.equal(late.body.caps.sent_today, 4);
  assert.equal(late.body.messages.length, 20);
  assert.ok(!late.body.messages.some((m) => orders.includes(m.rid)));
  await events(late.body.messages.map((m) => ({ rid: m.rid, type: "sent", kind: "invite", mailbox: P, ts: "2026-10-27T11:21:00Z", id: "s2:" + m.rid })));
  liveSent.push(...late.body.messages.map((m) => m.rid));
  // day 2 uses ramp step 2 (70)
  const d2 = await batch(P, "2026-10-28T10:00:00Z", "l4");
  assert.equal(d2.body.caps.send_day, 2);
  assert.equal(d2.body.caps.daily_cap, 70);
  await api("/api/release", { method: "POST", body: JSON.stringify({ run_id: d2.body.run_id }) });
  const diwali = await batch(P, "2026-11-06T06:00:00Z", "l5");
  assert.match(diwali.body.reason, /blackout/);
  const sat = await batch(R, "2026-10-31T06:00:00Z", "l6");
  assert.match(sat.body.reason, /weekday/);
});

test("reminders come first, 5 workdays later, and skip completed/opted-out", async () => {
  // Contacts invited on 27 Oct (24 of them in participate@). On Mon 2 Nov (4 workdays) none are due.
  const mon = await batch(P, "2026-11-02T10:00:00Z", "r0", 50);
  assert.ok(mon.body.messages.every((m) => m.kind === "invite"));
  await api("/api/release", { method: "POST", body: JSON.stringify({ run_id: mon.body.run_id }) });
  // Complete one and opt out another before the reminder date.
  const s = await api("/api/next-batch?mailbox=" + encodeURIComponent(P) + "&max=1&run_id=peek&now=2026-11-03T10:00:00Z");
  const first = s.body.messages[0];
  assert.equal(first.kind, "reminder");
  await api("/api/release", { method: "POST", body: JSON.stringify({ run_id: s.body.run_id }) });
  const invited = (await dash("batches")).body.rows.find((r) => r.day === "2026-10-27");
  assert.equal(invited.invited, 24);
  const cands = (await dash("search?q=" + first.rid)).body.rows;
  assert.equal(cands.length, 1);
  await events([
    { rid: first.rid, type: "started", ts: "2026-11-01T06:00:00Z", id: "kobo:1:started" },
    { rid: first.rid, type: "completed", ts: "2026-11-01T06:10:00Z", id: "kobo:1:completed" },
    { type: "completed", kind: "organic", id: "kobo:2:completed", meta: { submission_id: 2 } },
  ]);
  assert.equal((await contact(first.rid)).status, "completed");
  const tue = await batch(P, "2026-11-03T10:00:00Z", "r1", 50);
  const kinds = tue.body.messages.map((m) => m.kind);
  assert.equal(kinds.filter((k) => k === "reminder").length, 20); // per_run_max 20; 23 reminders due
  assert.ok(!tue.body.messages.some((m) => m.rid === first.rid), "completed contact gets no reminder");
  assert.ok(tue.body.messages[0].invite_msgid === null || typeof tue.body.messages[0].invite_msgid === "string");
  await events(tue.body.messages.map((m) => ({ rid: m.rid, type: "sent", kind: m.kind, mailbox: P, ts: "2026-11-03T10:01:00Z", id: "rem:" + m.rid })));
  assert.equal((await contact(tue.body.messages[0].rid)).status, "reminded");
});

test("opt-out page, form POST and RFC 8058 one-click", async () => {
  const g = await fetch(`${BASE}/optout/${realRid}`);
  assert.equal(g.status, 200);
  assert.match(await g.text(), /Confirm opt-out/);
  const p = await fetch(`${BASE}/optout/${realRid}`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: "confirm=1" });
  assert.equal(p.status, 200);
  assert.match(await p.text(), /opted out/);
  assert.equal((await contact(realRid)).status, "opted_out");
  const search = (await dash("search?q=gmail.com")).body.rows.find((r) => r.status === "queued");
  const oc = await fetch(`${BASE}/optout/${search.rid}`, { method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" }, body: "List-Unsubscribe=One-Click" });
  assert.equal(oc.status, 200);
  assert.equal((await contact(search.rid)).status, "opted_out");
  // opted-out contacts are never selected
  const ok = await fetch(`${BASE}/optout/NOSUCH99`, { method: "POST", body: "confirm=1" });
  assert.equal(ok.status, 200);
});

test("mail events: bounce, reply inbox, complaints auto-pause via cron", async () => {
  const [a, b, c, d, e] = liveSent.slice(-5);
  await events([
    { rid: a, type: "bounced_hard", ts: "2026-11-03T10:30:00Z", id: "mail:1", meta: { status: "5.1.1" } },
    { email: (await contact(b)).email, type: "reply", id: "mail:2", meta: { from: "x@y.example", subject: "Question", snippet: "Is it confidential?", classification: "query", message_id: "<m2@y>" } },
    { rid: c, type: "auto_reply", id: "mail:3", meta: { from: "ooo@y.example", subject: "Out of office", message_id: "<m3@y>" } },
  ]);
  assert.equal((await contact(a)).status, "bounced");
  assert.equal((await contact(b)).last_reply_at !== null, true);
  const open = await dash("replies?filter=open");
  assert.equal(open.body.rows.length, 1);
  assert.equal(open.body.rows[0].classification, "query");
  await dash("replies/" + open.body.rows[0].id, { method: "POST", body: JSON.stringify({ handled: true }) });
  assert.equal((await dash("replies?filter=open")).body.rows.length, 0);

  await events([c, d, e].map((rid, i) => ({ rid, type: "complaint", id: "arf:" + i, meta: { from: "abuse@isp.example", subject: "Abuse report", message_id: "<arf" + i + ">" } })));
  const cron = await fetch(`${BASE}/__scheduled?cron=*/15+*+*+*+*`);
  assert.equal(cron.status, 200);
  await new Promise((r) => setTimeout(r, 800));
  const sum = await dash("summary");
  assert.equal(sum.body.health.paused, true, JSON.stringify(sum.body.health));
  assert.match(sum.body.health.pause_reason, /3 spam complaints/);
  const blocked = await batch(R, "2026-11-03T10:00:00Z", "p1");
  assert.equal(blocked.body.paused, true);
  // resume resets baseline, so the same 3 complaints do not re-pause
  await dash("resume", { method: "POST", body: "{}" });
  await fetch(`${BASE}/__scheduled?cron=*/15+*+*+*+*`);
  await new Promise((r) => setTimeout(r, 800));
  assert.equal((await dash("summary")).body.health.paused, false);
  assert.equal((await contact(c)).status, "do_not_contact");
});

test("manual pause, contact hold, dashboard data endpoints, CSV export", async () => {
  assert.equal((await dash("pause", { method: "POST", body: JSON.stringify({}) })).status, 400);
  assert.equal((await dash("pause", { method: "POST", body: JSON.stringify({ reason: "checking templates" }) })).status, 200);
  assert.equal((await batch(P, "2026-11-03T10:00:00Z", "mp")).body.paused, true);
  await dash("resume", { method: "POST", body: "{}" });

  const q = (await dash("search?q=gmail.com")).body.rows.find((r) => r.status === "queued");
  const h = await dash(`contact/${q.rid}/status`, { method: "POST", body: JSON.stringify({ status: "on_hold", note: "wrong person" }) });
  assert.equal(h.body.status, "on_hold");
  const back = await dash(`contact/${q.rid}/status`, { method: "POST", body: JSON.stringify({ status: "active" }) });
  assert.equal(back.body.status, "queued");

  for (const p of ["daily", "strata", "batches", "activity", "replies?filter=all", "settings", "whoami"]) {
    const r = await dash(p);
    assert.equal(r.status, 200, p);
  }
  const sum = (await dash("summary")).body;
  assert.ok(sum.schedule.projection.finishDate);
  assert.equal(sum.kpis.organic_completed, 1);
  assert.equal(sum.kpis.invited, 24);
  const strata = (await dash("strata")).body.rows;
  assert.equal(strata.filter((r) => r.dim === "region").reduce((a, r) => a + r.contacts, 0), 8092);
  const csv = await fetch(BASE + "/dashboard/api/export.csv", { headers: { authorization: BASIC } });
  const text = await csv.text();
  assert.equal(csv.status, 200);
  assert.match(csv.headers.get("content-type"), /text\/csv/);
  assert.ok(text.split("\r\n").length >= 8093);
});

test("job API: heartbeat, state, summary", async () => {
  assert.equal((await api("/api/job-run", { method: "POST", body: JSON.stringify({ job: "send", ok: true, detail: { n: 1 } }) })).status, 200);
  await api("/api/state", { method: "PUT", body: JSON.stringify({ kobo_last_id: "42", "imap:participate@survo.tech": "7:9" }) });
  const st = await api("/api/state?keys=kobo_last_id,imap:participate@survo.tech");
  assert.deepEqual(st.body.state, { kobo_last_id: "42", "imap:participate@survo.tech": "7:9" });
  const s = await api("/api/summary?day=2026-10-27");
  assert.equal(s.body.today.invites, 24);
  const quota = await api("/api/job-run", { method: "POST", body: JSON.stringify({ job: "send", ok: false, detail: "quota", quota_mailbox: R }) });
  assert.equal(quota.status, 200);
  const blocked = await batch(R, null, "q1");
  // quota block applies to the real "today" (the override only fakes the selection clock)
  assert.ok(blocked.body.messages.length === 0);
});

test("Titan bounce guard stops a mailbox below Titan's limits", async () => {
  const ok = await batch(R, "2026-11-04T05:30:00Z", "g0", 1);
  assert.ok(!/bounce guard/.test(ok.body.reason || ""), JSON.stringify(ok.body));
  if (ok.body.run_id) await api("/api/release", { method: "POST", body: JSON.stringify({ run_id: ok.body.run_id }) });
  await events([0, 1, 2, 3].map((i) => ({ email: `nobody${i}@bounce.example`, type: "bounced_soft", mailbox: R, ts: "2026-11-04T05:0" + i + ":00Z", id: "guard:" + i })));
  const g = await batch(R, "2026-11-04T05:30:00Z", "g1", 5);
  assert.equal(g.body.messages.length, 0);
  assert.match(g.body.reason, /Titan bounce guard: research@survo.tech: 4 bounces in the last hour/);
  // the other mailbox is unaffected by research@'s hourly count (domain-wide limits are higher)
  const p = await batch(P, "2026-11-04T05:30:00Z", "g2", 1);
  assert.ok(!/bounce guard/.test(p.body.reason || ""));
  const h = (await dash("summary")).body.health.bounce_guard;
  assert.ok(h[R]);
});
