# IBLS outreach and tracking system (survo.tech)

This repo sends the invitations and reminders for the **India Business Leaders Survey** (LBS / MIT / Harvard, ethics ref. REC1101; fieldwork by Morsel Research and Development Pvt Ltd) and tracks the results. It covers about 8,092 companies. It runs entirely on free tiers:

* **Cloudflare Worker + D1** (`ibls`) serves the public site, click tracking, opt-out, the researcher dashboard, the job API, and the scheduling brain.
* **GitHub Actions** run the jobs: SMTP sending through Titan, IMAP bounce and reply processing, KoboToolbox sync, and a daily email report.

> **Safety default:** the setting `test_mode` starts as **true**. While it is on, the sender only ever emails the addresses in `test_recipients`. No real contact gets mail until a researcher switches test mode off on the dashboard.

---

## Architecture

```
                         ┌────────────────────────── Cloudflare (zone survo.tech) ──────────────────────────┐
  Invitee's mail client  │  Worker "ibls-survo"  (custom domains survo.tech, www.survo.tech)                │
  ───────────────────────┼─▶ /                static site (assets: site/)                                  │
   click  survo.tech/s/RID ─▶ /s/:rid        log click ─▶ 302 ee.kobotoolbox.org/jbh8Urj6?d[rid]=RID ──────┼──▶ KoboToolbox form
   opt-out link / 1-click ─▶ /optout/:rid    confirm page, form POST, RFC 8058 One-Click POST             │      (hidden field rid)
                         │                                                                                   │
  Researchers ──Access──▶ /dashboard  (+ /dashboard/api/*)  KPIs, charts, strata, batches, replies,         │
   (Cloudflare Access    │                                   search + timeline, pause/resume, settings, CSV │
    or Basic auth)       │                                                                                   │
                         │  /api/*  (Bearer API_TOKEN)  next-batch · events · job-run · state · summary     │
                         │  cron */15  stop rules (complaints, bounce rate, heartbeat) · release stale       │
                         │             reservations · daily rollups                                          │
                         │  D1 "ibls": contacts · events · replies · settings · job_runs · daily_stats       │
                         └──────────────▲───────────────────────────▲────────────────────────▲──────────────┘
                                        │ HTTPS + Bearer             │                        │
  GitHub Actions (Python 3.12) ─────────┴───────────────────────────┴────────────────────────┘
   send.yml          every 10 min, 04:30–11:30 UTC Mon–Fri: next-batch ─▶ Titan SMTP 465 (both mailboxes,
                     20–60 s jitter) ─▶ events; stops on 5.4.x/4.7.x quota errors
   sync.yml          every 30 min: IMAP 993 both mailboxes (DSN bounces, ARF complaints, auto-replies,
                     opt-outs, queries) + Kobo API v2 submissions (started/completed/declined) ─▶ events
   daily-report.yml  12:30 UTC: summary email from research@ to REPORT_TO
   deploy.yml        push to main: tests ─▶ D1 migrations ─▶ wrangler deploy ─▶ sync Worker secrets
   ci.yml            tests on PRs
```

All scheduling decisions are made in the Worker: pause, test mode, window, blackout dates, ramp caps, hourly cap, pacing, reminder timing, Titan bounce guard, and ordering. The sender only asks "what is due for this mailbox now?". You can change everything from the dashboard without redeploying.

## Repository layout

```
src/                Worker (TypeScript): index.ts router, api.ts (job API + next-batch), dashboard.ts,
                    public.ts (/s, /optout), cron.ts (stop rules), schedule.ts (pure date/cap logic),
                    db.ts (settings + event ingestion), stats.ts, auth.ts (Access JWT / Basic / Bearer)
dashboard/          dashboard.html (bundled into the Worker as text; never a public file)
site/               public study page (standalone; hero.webp 85 KB), robots.txt, _headers
migrations/         D1 schema + default settings
templates/          Jinja2 email templates (invite/reminder, text + HTML)
jobs/               send.py, sync_mail.py (+ mailclass.py), sync_kobo.py, daily_report.py, common.py, render.py
scripts/            import_contacts.py, seed_test.py, smoke_test.sh, local_e2e.sh, hash_password.py
kobo/               Self_india_business_leaders_survey_v2_rid.xlsx (hidden `rid` added) + build script
tests/              Python unit tests (mocked SMTP/IMAP/Kobo) + integration test against local Worker
test/               Node tests: schedule.test.mjs (unit) and e2e.mjs (HTTP end-to-end on wrangler dev)
.github/workflows/  send, sync, daily-report, deploy, ci
```

Contact data is personal data and **is not in this repo**. Keep the tracker in `data/`, which git ignores.

---

## Secrets and variables

| Where | Name | What |
|---|---|---|
| GitHub secret | `CLOUDFLARE_API_TOKEN` | API token with **Workers Scripts: Edit**, **Workers Routes: Edit** (zone survo.tech), **D1: Edit**, **Account Settings: Read**, **Zone: Read**, **DNS: Edit** (for the custom domains). The "Edit Cloudflare Workers" template plus D1 Edit is enough. |
| GitHub secret | `CLOUDFLARE_ACCOUNT_ID` | Cloudflare account id |
| GitHub secret **and** Worker secret | `API_TOKEN` | Random string of at least 16 characters (use `openssl rand -hex 32`). The Worker accepts `/api/*` calls only with `Authorization: Bearer <API_TOKEN>`. `deploy.yml` copies it into the Worker. |
| GitHub secret | `SMTP_PASS_PARTICIPATE` | Titan password for participate@survo.tech (used for SMTP and IMAP) |
| GitHub secret | `SMTP_PASS_RESEARCH` | Titan password for research@survo.tech |
| GitHub secret | `KOBO_TOKEN` | KoboToolbox API key (Account settings → Security → API key) |
| GitHub secret | `KOBO_ASSET_UID` | The form's asset UID: the `a…` id in `kf.kobotoolbox.org/#/forms/<UID>/…` |
| GitHub secret | `REPORT_TO` | Comma-separated list of daily report recipients |
| GitHub secret **and** Worker secret | `DASHBOARD_USERS` | Fallback Basic-auth users, `user:sha256hex,user2:sha256hex`. Generate entries with `python3 scripts/hash_password.py alice`. `deploy.yml` copies it into the Worker. |
| GitHub variable | `D1_DATABASE_ID` | The id printed by `wrangler d1 create ibls`. `deploy.yml` writes it into wrangler.toml; you can also commit it there (it is not a secret). |
| GitHub variable (optional) | `API_BASE` (default https://survo.tech), `SAVE_SENT` (`1` = IMAP-append sent copies to "Sent"), `FILE_NOISE` (`1` = move bounces and auto-replies to IBLS/* folders), `IMAP_SINCE` (first-run IMAP date, default `20-Oct-2026`), `KOBO_API_BASE` (default https://kf.kobotoolbox.org) |
| wrangler.toml `[vars]` | `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD` | Cloudflare Access team domain (e.g. `morsel.cloudflareaccess.com`) and the Access app's AUD tag. Used for in-Worker JWT verification. |
| wrangler.toml `[vars]` | `SITE_ORIGIN`, `KOBO_FORM_URL` | `https://survo.tech`, `https://ee.kobotoolbox.org/jbh8Urj6` |

The repo contains no secrets. `.dev.vars` (local only) is git-ignored. `.dev.vars.example` contains test values.

---

## Setup checklist

1. **Email authentication (before any sending).** In Cloudflare DNS for survo.tech, add or check:
   * MX records from Titan's setup page.
   * SPF: `v=spf1 include:spf.titan.email ~all` (exactly one SPF record).
   * DKIM: the TXT record from Titan admin → Domain → DKIM.
   * DMARC: `_dmarc` TXT `v=DMARC1; p=none; rua=mailto:research@survo.tech` (tighten to `quarantine` later).

   Gmail and Yahoo require SPF, DKIM and DMARC alignment and working one-click unsubscribe for bulk senders. The sender adds `List-Unsubscribe` and `List-Unsubscribe-Post` headers.
2. **Remove any existing `A`, `AAAA` or `CNAME` records for `survo.tech` and `www`**, for example from the old hosted page. A Worker custom domain cannot be created over them. Leave MX and TXT records alone.
3. `npm ci`, then `pip install -r requirements-dev.txt`.
4. `npx wrangler login` (or export `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`).
5. `npx wrangler d1 create ibls`. Put the printed `database_id` into `wrangler.toml`, or into the GitHub variable `D1_DATABASE_ID`.
6. `npx wrangler d1 migrations apply ibls --remote`
7. **Import contacts:** copy `IBLS_outreach_tracker.xlsx` to `data/`, then run
   `python3 scripts/import_contacts.py --tracker data/IBLS_outreach_tracker.xlsx --apply --remote`
   This imports all 8,092 rows: 7,969 queued and 123 `on_hold` (shared consultant addresses), with region, size band, nature, email tier, a stratified `send_order` and alternating mailboxes. It writes 17 SQL files and takes about 1 minute. Re-running it is safe because it never resets progress.
8. **Worker secrets:** `npx wrangler secret put API_TOKEN` and `npx wrangler secret put DASHBOARD_USERS`. `deploy.yml` also syncs them from GitHub secrets.
9. **First deploy:** `npx wrangler deploy`. This creates the custom domains survo.tech and www.survo.tech with certificates. Then run `BASE=https://survo.tech API_TOKEN=… scripts/smoke_test.sh`.
10. **Cloudflare Access** (see below), then fill in `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD` in wrangler.toml and push.
11. **KoboToolbox:** in the project → Form → Replace form, upload `kobo/Self_india_business_leaders_survey_v2_rid.xlsx`, then **Redeploy**. The form_id is unchanged, so the link `ee.kobotoolbox.org/jbh8Urj6` keeps working. Check that `https://ee.kobotoolbox.org/jbh8Urj6?d[rid]=TEST0001` produces a submission with `rid = TEST0001` (Data → Table).
12. **GitHub:** push this repo to GitHub (see the note on minutes below), add the secrets and variables, and enable Actions. Pushing to `main` deploys.
13. **Test run (real email, allow-listed addresses only):**
    `make seed-test EMAILS=you@example.com,colleague@example.org`
    This creates TEST0001… contacts and sets `test_recipients`, keeping test mode on. Then go to Actions → **send** → Run workflow. In test mode the calendar window is ignored, so this works any day. Check:
    * Both messages arrive, the text and HTML look right, and the From and Reply-To are correct.
    * The `/s/TEST000x` link opens Kobo with the rid filled in.
    * The opt-out link works, and Gmail shows its "Unsubscribe" button.
    * Replying "OPT OUT" or a question shows up on the dashboard after the next **sync** run.
    * Submitting the form marks the contact completed after sync.

    Run `make seed-test` again to reset the test contacts.
14. **Go live** before 27 Oct 2026, 10:00 IST: on the dashboard, set `postal_address` if you want one in the footer, review the caps (see the Titan limits below), then set **test_mode = false**. Nothing goes out before `start_date`, outside 10:00–17:00 IST Mon–Fri, or on blackout dates.

### GitHub Actions minutes

Sending at 20–60 s jitter takes about 200 runner-minutes per send day. That is roughly 4,000–9,000 minutes a month, counting each 10-minute run at up to 10 minutes. **Public repositories get unlimited free minutes on standard runners**, so put this repo in a public GitHub repo. It is safe to publish: there are no secrets or contact data in it. A private repo on the free plan has 2,000 minutes a month and would run out mid-campaign.

### Titan sending limits (check your plan)

Per mailbox, to external addresses, from Titan's own help pages:

| Plan | Per hour | Per day |
|---|---|---|
| Starter | 50 | 300 |
| Pro | 200 | 500 |
| Premium | 300 | 1,000 |

Bounce limits per mailbox are **5 per hour, 10 per day and 20 per 3 days**. Per domain they are 10 per hour, 20 per day and 50 per 3 days on paid plans.

* The default `hourly_cap = 120` and `cap_max = 300` come from the brief. **On a Starter plan, set `hourly_cap` ≤ 45 and `cap_max` ≈ 280** on the dashboard, to leave room for researchers' own replies and the daily report from research@. Even pacing already keeps the normal rate at about 43 per hour at cap 300.
* The Worker includes a **Titan bounce guard** (settings `bounce_guard_*`, defaults 4/8/18 per mailbox and 18/45 per domain). It stops a mailbox before Titan does, and the mailbox resumes when the rolling window clears. These limits mean that **list quality matters more than the 3% stop rule**: at 300 sends a day, a 3% bounce rate would hit Titan's mailbox limits within a day.

---

## Cloudflare Access (primary dashboard protection)

1. Go to Cloudflare dashboard → **Zero Trust**. Pick a team name such as `morsel`; the free plan covers up to 50 users.
2. Settings → Authentication: add a login method. One-time PIN by email is fine, or Google.
3. Access → Applications → **Add an application → Self-hosted**:
   * Name: `IBLS dashboard`. Domain: `survo.tech`, path `dashboard`. Add a second destination for `survo.tech/dashboard/*`.
   * Session duration: 24 h.
4. Add a policy **Allow → Include → Emails**: the researchers' addresses, or an email domain such as `london.edu`, `mit.edu`, `fas.harvard.edu` or the Morsel domain.
5. Save. Copy the application's **AUD tag** (Overview tab) into `ACCESS_AUD`, and `<team>.cloudflareaccess.com` into `ACCESS_TEAM_DOMAIN` in wrangler.toml. Then deploy.
6. Make sure `/api/*`, `/s/*`, `/optout/*` and `/` are **not** covered by the Access app.

The Worker verifies the `Cf-Access-Jwt-Assertion` signature, audience and issuer against your team's keys. If no valid Access JWT arrives, it falls back to HTTP Basic against `DASHBOARD_USERS`. If neither is configured, it refuses with 503 (fail closed). State-changing dashboard calls also require an `X-IBLS-Dashboard: 1` header as CSRF protection.

---

## Scheduling rules

All of these are settings you can edit on the dashboard:

| Setting | Default | Meaning |
|---|---|---|
| `start_date` / `end_date` | 2026-10-27 / (none) | No sends before `start_date` or after `end_date` (IST dates) |
| `window_start` / `window_end` | 10:00 / 17:00 | IST send window |
| `send_weekdays` | 1,2,3,4,5 | Mon–Fri |
| `blackout_dates` | 2026-11-05..2026-11-11 | Diwali. Accepts a comma list of dates or `a..b` ranges. |
| `ramp` / `cap_max` | 40,70,110,160,220,300 / 300 | Daily cap per mailbox on its 1st, 2nd, … actual send day; the last value repeats |
| `hourly_cap` | 120 | Per mailbox, over a rolling 60 minutes |
| `per_run_max` | 20 | Per mailbox per 10-minute run. The sender also limits itself to what fits in a 9-minute run at the jitter it uses (about 14). |
| `pace_evenly` | true | Spreads the daily cap across the window instead of sending it all in the morning |
| `reminder_after_workdays` | 5 | One reminder on the 5th workday after the invite. Workdays skip weekends and blackout dates. |
| `reminder2_enabled` / `reminder2_after_workdays` | false / 5 | Optional second reminder |
| `test_mode` / `test_recipients` | **true** / empty | Allow-list only, and the calendar is ignored |
| `complaint_threshold` | 3 | Auto-pause at 3 or more spam complaints. Resuming resets the baseline, so only a new complaint pauses again. |
| `bounce_rate_threshold` / `bounce_window` / `bounce_min_sample` | 0.03 / 200 / 50 | Auto-pause if more than 3% of the last 200 sends hard-bounced, once there are at least 50 sends |
| `heartbeat_max_minutes` | 90 | Auto-pause if no sender run is seen for 90 minutes during an open window while work is due. GitHub cron can lag 15–60 minutes at busy times, so do not set this much lower. |
| `bounce_guard_*` | 4/8/18 and 18/45 | Titan bounce guard (above) |
| `postal_address`, `from_name` | empty / India Business Leaders Survey | Footer address and display name |
| `target_end_date` | 2026-12-18 | Used only to compare against the projected finish on the dashboard |

**Order:** reminders always go first, then new invites in `send_order`. `send_order` is a stratified-random interleave over region × size band × nature × email tier, computed at import. The first 80 invites match the full sample's mix to within 2 percentage points on every dimension.

**Who never gets mail:** contacts with status `on_hold`, `bounced`, `opted_out` or `do_not_contact`, and anyone who completed the survey. Anyone who declined consent, complained, or hard-bounced gets no reminders.

**Projection:** with the defaults, the last invites go out around 9 Dec and the last reminders around 16 Dec 2026. That is 32 send days. The dashboard recomputes this live from the remaining work.

---

## Operating it

### How to pause

* **Dashboard:** click **Pause sending** and enter a reason (required). This takes effect on the next sender run, within 10 minutes. The current run finishes the message it is on. Click **Resume** to continue; the stop-rule counters restart from that moment.
* **Emergency without the dashboard:** disable the `send` workflow (Actions → send → ⋯ → Disable workflow). Or run `npx wrangler d1 execute ibls --remote --command "UPDATE settings SET value='true' WHERE key='paused'"`.
* **Auto-pauses** show as a red banner with their reason: complaints, bounce rate, or missing heartbeat. A mailbox that hits a Titan quota error stops for the rest of the IST day on its own.

### How to add contacts

Put the new contacts in a CSV with columns `email,company` and optionally `rid,salutation,director_name,cin,state,region,size_band,nature,email_tier,peer_tier,status,notes`. Then run:

```
python3 scripts/import_contacts.py --csv data/new_contacts.csv --order-offset 100000 --apply --remote
```

RIDs are generated when missing. The script refuses to run on duplicate emails or RIDs. `--order-offset` queues the new contacts after the current queue. Existing contacts are only updated in their descriptive fields, never in their progress. To put one contact on hold or mark it do-not-contact, use the dashboard (search → contact → actions).

### Runbook

| Situation | What happens automatically | What you do |
|---|---|---|
| **Hard bounce** (DSN 5.x.x, or the recipient is refused at SMTP time) | Contact → `bounced`, no further mail. Counts toward the bounce-rate stop rule and the Titan guard. | Nothing for a single bounce. If the dashboard shows a high rate for one send day or stratum, pause and check that slice of the list. If a better address exists, add it as a new contact. |
| **Soft bounce** (4.x.x, mailbox full) | Recorded. After 3 soft bounces the contact becomes `bounced`. A soft-bounced contact still gets its reminder. | Nothing usually. |
| **Spam complaint** (ARF) | Contact → `do_not_contact`. **3 complaints pauses everything.** | Read the complaint, check that the templates and footer are intact, and resume only when you are confident. More than 5 complaints risks Titan suspending the account. |
| **Opt-out** (link, one-click, or reply "OPT OUT", "unsubscribe", "remove", "stop", "not interested") | Contact → `opted_out`, permanently. The reply shows as informational. | Nothing. |
| **Query / human reply** | Appears under **Replies needing attention** (the mail stays unread in the inbox) | Answer from Titan webmail, then click **Mark handled**. Wrong person but a referral given: put the contact on hold and add the referred person as a new contact. |
| **Auto-reply / out of office** | Shown as handled. Moved to IBLS/Auto-replies if `FILE_NOISE=1`. | Look occasionally for "has left the company, contact X" replies. |
| **"Titan bounce guard" reason in health** | The mailbox stops until the rolling window clears | Usually nothing. If it keeps happening, lower the caps. |
| **SMTP quota error (5.4.x / 4.7.x)** | The mailbox stops for the rest of the IST day; the job is marked as an error | Lower `cap_max` or `hourly_cap` to match your Titan plan. |
| **Heartbeat auto-pause** | Sending is paused | Check Actions → send. Look for a failed run, an expired secret, or disabled workflows; GitHub disables scheduled workflows in repos with no activity for 60 days. Fix it, then resume. |
| **Auth failure** | Job error, mailbox skipped | Update `SMTP_PASS_*`; Titan passwords may have been rotated. |

Mail is **never deleted**. IMAP reading uses `BODY.PEEK`, so messages stay unread for researchers. With `FILE_NOISE=1`, bounces and auto-replies are moved using IMAP MOVE, never deleted.

---

## Data captured

* `/s/RID` logs a click with time, user agent and country. Clicks are tagged `human` or `bot`; link scanners such as Proofpoint, Mimecast and SafeLinks prefetch links, so CTR counts human-looking clicks only. It then redirects to `https://ee.kobotoolbox.org/jbh8Urj6?d[rid]=RID`. An unknown RID redirects without the parameter.
* Kobo stores only submitted forms, so `started` means any submission. `completed` means consent was "1" and the form has an `end` time. Consent "0" is recorded as `consent_declined`. Submissions without a rid are counted as **organic**.
* There is no tracking pixel. Opens are not measured.

## Testing

| Command | What it covers |
|---|---|
| `npx tsc --noEmit` | Worker type check |
| `node --test test/*.test.mjs` | Scheduling unit tests: IST conversion, window, blackout, ramp, pacing, workday reminder cutoff, finish-date projection |
| `python3 -m unittest discover -s tests -t .` | 36 tests: exact template text, headers, multipart and no attachments, the send loop (hard and soft failures, quota stop, reconnect, time budget, test-mode guard, dry run, stop if events cannot be recorded), mail classification (DSN hard, soft and delayed, non-standard bounces, ARF, OOO, opt-out with quoted footer, queries), the IMAP sync cursor (UIDVALIDITY, PEEK, no deletes, MOVE), Kobo mapping and pagination, the daily report, import ordering and SQL, seed SQL |
| `scripts/local_e2e.sh` (or `make e2e`) | Fresh local D1 → migrations → **import of all 8,092 contacts** → seed → `wrangler dev --local` → the real Python jobs against the Worker (SMTP, IMAP and Kobo faked) → 12 HTTP end-to-end tests: static site, auth, click redirect, test-mode isolation and idempotent reservations, live window, start date, ramp and pacing, blackout, reminders, opt-out (form and one-click), bounces, replies, complaint auto-pause via the cron trigger and resume, manual pause, hold, dashboard data, CSV export, job API, Titan bounce guard |
| `scripts/smoke_test.sh` | Read-only checks after a deploy |

`.dev.vars.example` sets `ALLOW_TIME_OVERRIDE=1` so local tests can fake the clock (`?now=`). Never set it in production.
