# IBLS outreach and tracking system (survo.tech)

This repo sends the invitations and reminders for the **India Business Leaders Survey** (LBS / MIT / Harvard, ethics ref. REC1101; fieldwork by Morsel Research and Development Pvt Ltd) and tracks the results. It covers about 8,092 companies. Fieldwork contact: **Bishnu Datta Behuria, Morsel Research and Development, bishnu@morselindia.org**.

* **Cloudflare Worker + D1** (`ibls`) serves the public site, click tracking, opt-out, the BIMI logo, the researcher dashboard, the job API, and the scheduling brain (including each mailbox's warm-up).
* **Google Workspace (Gmail)** on survo.tech sends the mail from five rotating mailboxes: `participate@`, `research@`, `survey@`, `study@` and `team@survo.tech`.
* **GitHub Actions** run the jobs: sending through the **Gmail API** (a service account with domain-wide delegation impersonates each mailbox), Gmail API reading of replies and bounces, KoboToolbox sync, and a daily email report. A fallback mode (`MAIL_TRANSPORT=smtp`) uses smtp.gmail.com / imap.gmail.com with app passwords.

> **Safety default:** the setting `test_mode` starts as **true**. While it is on, the sender only ever emails the addresses in `test_recipients`. No real contact gets mail until a researcher switches test mode off on the dashboard. Even then each mailbox starts at warm-up **stage 0 (seed-test day)**, when it may only send to the seeded test addresses.

---

## Architecture

```
                         ┌────────────────────────── Cloudflare (zone survo.tech) ──────────────────────────┐
  Invitee's mail client  │  Worker "ibls-survo"  (custom domains survo.tech, www.survo.tech)                │
  ───────────────────────┼─▶ /                static site (assets: site/)                                  │
   click  survo.tech/s/RID ─▶ /s/:rid        log click ─▶ 302 ee.kobotoolbox.org/jbh8Urj6?d[rid]=RID ──────┼──▶ KoboToolbox form
   opt-out link / 1-click ─▶ /optout/:rid    confirm page, form POST, RFC 8058 One-Click POST             │      (hidden field rid)
   BIMI lookup            ─▶ /bimi/logo.svg  SVG Tiny PS, Content-Type image/svg+xml                       │
                         │                                                                                   │
  Researchers ──Access──▶ /dashboard  (+ /dashboard/api/*)  KPIs, charts, strata, batches, replies,         │
   (Cloudflare Access    │                                   per-mailbox warm-up, pause/resume, settings,   │
    or Basic auth)       │                                   search + timeline, CSV                         │
                         │  /api/*  (Bearer API_TOKEN)  next-batch · events · job-run · state · summary ·    │
                         │                              mailboxes                                            │
                         │  cron */15  stop rules (complaints, bounce rate, heartbeat) · release stale       │
                         │             reservations · daily rollups                                          │
                         │  D1 "ibls": contacts · events · replies · settings (+ state) · job_runs · stats   │
                         └──────────────▲───────────────────────────▲────────────────────────▲──────────────┘
                                        │ HTTPS + Bearer             │                        │
  GitHub Actions (Python 3.12) ─────────┴───────────────────────────┴────────────────────────┘
   send.yml          every 10 min, 04:30–11:30 UTC Mon–Fri: 5 mailboxes in parallel, each: next-batch ─▶
                     Gmail API users.messages.send as that mailbox (20–60 s jitter) ─▶ events;
                     stops a mailbox on daily-quota / rate-limit / auth errors
   sync.yml          every 30 min: Gmail API history (or query) per mailbox: DSN bounces, ARF complaints,
                     auto-replies, opt-outs, queries ─▶ events; + Kobo API v2 submissions ─▶ events
   daily-report.yml  12:30 UTC: summary email from research@ to REPORT_TO (incl. per-mailbox warm-up)
   deploy.yml        push to main: tests ─▶ D1 migrations ─▶ wrangler deploy ─▶ sync Worker secrets
   ci.yml            tests on PRs
```

All scheduling decisions are made in the Worker: pause, test mode, window, blackout dates, per-mailbox warm-up caps, hourly cap, pacing, reminder timing, bounce guard, and ordering. The sender only asks "what is due for this mailbox now?". You can change everything from the dashboard without redeploying.

**Rotation.** Invites come from one **shared queue** in stratified `send_order`: every 10 minutes each of the five mailboxes takes what its own warm-up cap and pacing allow, so the load rotates across the mailboxes and a mailbox that is held back never strands contacts. A reminder always goes from the mailbox that sent that contact's invitation, threaded to it (`In-Reply-To`). Reservation is a single atomic `UPDATE … WHERE rid IN (SELECT … LIMIT n)`, so two mailboxes can never take the same contact. Every event (sent, bounce, reply, …) is stored together with its effect on the contact in one D1 batch (a transaction), so a failed request leaves nothing half-applied and the job's retry is safe.

**Headers.** `From: India Business Leaders Survey <the sending mailbox>` (the display name is the `from_name` setting, so it can be a researcher's name), `Reply-To:` the `reply_to` setting (default `research@survo.tech`), `List-Unsubscribe: <mailto:reply_to?subject=OPT OUT>, <https://survo.tech/optout/RID>` and `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058), plus `X-IBLS-RID`. Gmail keeps a copy of every message in the mailbox's Sent folder. If Gmail replaces our Message-ID, the sender reads back the one recipients see and records it, so reminders still thread and replies still match.

## Repository layout

```
src/                Worker (TypeScript): index.ts router (+ /bimi/logo.svg), api.ts (job API, next-batch, mailboxes),
                    warmup.ts (per-mailbox warm-up state), dashboard.ts, public.ts (/s, /optout), cron.ts (stop rules),
                    schedule.ts (pure date/cap/warm-up logic), db.ts (settings + event ingestion), stats.ts, auth.ts
dashboard/          dashboard.html (bundled into the Worker as text; never a public file)
site/               public study page (standalone; hero.webp 85 KB), bimi/logo.svg, robots.txt, _headers
migrations/         D1 schema + default settings (0002: warm-up, Reply-To, five mailboxes)
templates/          Jinja2 email templates (invite/reminder, text + HTML)
jobs/               send.py, sync_mail.py (+ mailclass.py), transport.py (Gmail API / SMTP / fake), gmail_api.py
                    (service-account JWT + Gmail REST), sync_kobo.py, daily_report.py, common.py, render.py
scripts/            import_contacts.py, seed_test.py, smoke_test.sh, local_e2e.sh, fake_full_run.{sh,py}, hash_password.py
kobo/               Self_india_business_leaders_survey_v2_rid.xlsx (hidden `rid` added) + build script
tests/              Python unit tests (mocked Gmail API / SMTP / IMAP / Kobo) + integration test against local Worker
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
| GitHub secret | `GOOGLE_SA_JSON` | The **whole JSON key** of the service account `ibls-mailer@ibls-mailer.iam.gserviceaccount.com` (paste the file's contents; base64 of it also works). Used by send, sync and the daily report in the default `gmail_api` mode. |
| GitHub secret (fallback only) | `GMAIL_APP_PW_PARTICIPATE`, `GMAIL_APP_PW_RESEARCH`, `GMAIL_APP_PW_SURVEY`, `GMAIL_APP_PW_STUDY`, `GMAIL_APP_PW_TEAM` | Google **app passwords** (16 characters, spaces are ignored) for each mailbox. Only needed with `MAIL_TRANSPORT=smtp`; requires 2-Step Verification on each mailbox. |
| GitHub secret | `KOBO_TOKEN` | KoboToolbox API key (Account settings → Security → API key) |
| GitHub secret | `KOBO_ASSET_UID` | The form's asset UID: the `a…` id in `kf.kobotoolbox.org/#/forms/<UID>/…` |
| GitHub secret | `REPORT_TO` | Comma-separated list of daily report recipients |
| GitHub secret **and** Worker secret | `DASHBOARD_USERS` | Fallback Basic-auth users, `user:sha256hex,user2:sha256hex`. Generate entries with `python3 scripts/hash_password.py alice`. `deploy.yml` copies it into the Worker. |
| GitHub variable | `D1_DATABASE_ID` | The id printed by `wrangler d1 create ibls`. `deploy.yml` writes it into wrangler.toml; you can also commit it there (it is not a secret). |
| GitHub variable (optional) | `MAIL_TRANSPORT` (`gmail_api` default, or `smtp`), `MAILBOXES` (default: the five mailboxes; must match the Worker's `MAILBOXES` var), `API_BASE` (default https://survo.tech), `FILE_NOISE` (`1` = label bounces/auto-replies `IBLS/*` and archive them out of the inbox, still unread), `MAIL_SINCE` (first-run mail date, default `2026-10-20`), `KOBO_API_BASE` (default https://kf.kobotoolbox.org) |
| wrangler.toml `[vars]` | `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD` | Cloudflare Access team domain (e.g. `morsel.cloudflareaccess.com`) and the Access app's AUD tag. Used for in-Worker JWT verification. |
| wrangler.toml `[vars]` | `SITE_ORIGIN`, `KOBO_FORM_URL`, `MAILBOXES` | `https://survo.tech`, `https://ee.kobotoolbox.org/jbh8Urj6`, the five sending mailboxes |

The repo contains no secrets. `.dev.vars` (local only) is git-ignored. `.dev.vars.example` contains test values.

---

## Gmail setup (Google Workspace, survo.tech)

The five mailboxes are ordinary Workspace users. The jobs never use their passwords: a Google Cloud **service account** is allowed, by **domain-wide delegation (DWD)**, to act as each of them for three Gmail scopes only.

1. **Service account** (Google Cloud console, project `ibls-mailer`): `ibls-mailer@ibls-mailer.iam.gserviceaccount.com`. Enable the **Gmail API** for the project. Create a JSON key (Service account → Keys → Add key → JSON) and store its contents as the GitHub secret `GOOGLE_SA_JSON`. Never commit it.
2. **Domain-wide delegation** (admin.google.com → Security → Access and data control → API controls → **Manage Domain Wide Delegation** → Add new):
   * Client ID: **`116272134472583167099`**
   * OAuth scopes (exactly these three, comma separated):
     `https://www.googleapis.com/auth/gmail.send,https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/gmail.modify`
3. **Mailboxes:** `participate@`, `research@`, `survey@`, `study@`, `team@survo.tech` must exist, have Gmail enabled, and be licensed. Each job impersonates them one by one (the JWT `sub`).
4. **Check:** run Actions → **send** with *dry run* ticked (renders, sends nothing), then the test run below. A delegation or scope mistake shows as an `auth` error for that mailbox in the job log and on the dashboard; the other mailboxes carry on.
5. **Fallback (only if the API route is unavailable):** turn on 2-Step Verification for each mailbox, create an app password per mailbox, store them as `GMAIL_APP_PW_<LOCALPART>`, and set the repository variable `MAIL_TRANSPORT=smtp`. Sending then uses `smtp.gmail.com:587` with STARTTLS and reading uses `imap.gmail.com:993`. App passwords may be disabled by Workspace policy; the API route is preferred.

**Email authentication (DNS, done in Cloudflare by whoever manages the zone; this repo adds no DNS):** MX to Google; SPF `v=spf1 include:_spf.google.com ~all` (one SPF record only); DKIM generated in Admin console → Apps → Google Workspace → Gmail → **Authenticate email** (TXT `google._domainkey`), then *Start authentication*; DMARC `_dmarc` `v=DMARC1; p=none; rua=mailto:research@survo.tech`, tightened to `quarantine` once reports are clean. Gmail and Yahoo require SPF, DKIM, DMARC alignment and one-click unsubscribe for bulk senders.

**BIMI:** the Worker serves `https://survo.tech/bimi/logo.svg` (SVG Tiny PS) with `Content-Type: image/svg+xml`. A BIMI record (`default._bimi` TXT `v=BIMI1; l=https://survo.tech/bimi/logo.svg;`) only takes effect with DMARC at `quarantine` or `reject`, and Gmail additionally requires a VMC or CMC certificate (`a=`). That DNS record is not part of this repo.

---

## Setup checklist

1. **Gmail and DNS** as above (service account, DWD, mailboxes, SPF/DKIM/DMARC).
2. **Remove any existing `A`, `AAAA` or `CNAME` records for `survo.tech` and `www`**, for example from the old hosted page. A Worker custom domain cannot be created over them. Leave MX and TXT records alone.
3. `npm ci`, then `pip install -r requirements-dev.txt`.
4. `npx wrangler login` (or export `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`).
5. `npx wrangler d1 create ibls`. Put the printed `database_id` into `wrangler.toml`, or into the GitHub variable `D1_DATABASE_ID`.
6. `npx wrangler d1 migrations apply ibls --remote`
7. **Import contacts:** copy `IBLS_outreach_tracker.xlsx` to `data/`, then run
   `python3 scripts/import_contacts.py --tracker data/IBLS_outreach_tracker.xlsx --apply --remote`
   This imports all 8,092 rows: 7,969 queued and 123 `on_hold` (shared consultant addresses), with region, size band, nature, email tier and a stratified `send_order`. It writes 17 SQL files and takes about 1 minute. Re-running it is safe because it never resets progress.
8. **Worker secrets:** `npx wrangler secret put API_TOKEN` and `npx wrangler secret put DASHBOARD_USERS`. `deploy.yml` also syncs them from GitHub secrets.
9. **First deploy:** `npx wrangler deploy`. This creates the custom domains survo.tech and www.survo.tech with certificates. Then run `BASE=https://survo.tech API_TOKEN=… scripts/smoke_test.sh`.
10. **Cloudflare Access** (see below), then fill in `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD` in wrangler.toml and push.
11. **KoboToolbox:** in the project → Form → Replace form, upload `kobo/Self_india_business_leaders_survey_v2_rid.xlsx`, then **Redeploy**. The form_id is unchanged, so the link `ee.kobotoolbox.org/jbh8Urj6` keeps working. Check that `https://ee.kobotoolbox.org/jbh8Urj6?d[rid]=TEST0001` produces a submission with `rid = TEST0001` (Data → Table).
12. **GitHub:** push this repo to GitHub (see the note on minutes below), add the secrets and variables, and enable Actions. Pushing to `main` deploys.
13. **Seed test = warm-up stage 0 (real email, allow-listed addresses only), on a weekday:**
    `make seed-test EMAILS=you@gmail.com,colleague@example.org PER_MAILBOX=1`
    With `PER_MAILBOX=1` every address gets one plus-addressed test contact per mailbox (`you+participate@gmail.com`, `you+research@gmail.com`, …), so every mailbox sends its seed. Without it, test contacts are spread round-robin over the mailboxes (five addresses cover all five). It keeps test mode on. Then go to Actions → **send** → Run workflow. In test mode the calendar window is ignored. Check:
    * A message from each mailbox arrives (inbox, not spam), the text and HTML look right, From shows the study name with the sending address, and Reply-To is research@survo.tech.
    * Gmail shows "Unsubscribe" next to the sender; the opt-out link works.
    * The `/s/TEST000x` link opens Kobo with the rid filled in.
    * Replying "OPT OUT" or a question shows up on the dashboard after the next **sync** run.
    * Submitting the form marks the contact completed after sync.

    A weekday with clean seed sends moves each mailbox to **stage 1 (20/day)** from the next day. Run `make seed-test` again to reset the test contacts.
14. **Go live** before 27 Oct 2026, 10:00 IST: on the dashboard, set `postal_address` if you want one in the footer, check the warm-up table (every mailbox should show stage 1 after its seed day; otherwise use **Set stage**), then set **test_mode = false**. Nothing goes out before `start_date`, outside 10:00–17:00 IST Mon–Fri, or on blackout dates (Diwali 5–11 Nov 2026).

### GitHub Actions minutes

Sending at 20–60 s jitter takes roughly 10 runner-minutes per 10-minute run during the window, i.e. about 400 runner-minutes per send day, or up to ~9,000 minutes a month. **Public repositories get unlimited free minutes on standard runners**, so put this repo in a public GitHub repo. It is safe to publish: there are no secrets or contact data in it. A private repo on the free plan has 2,000 minutes a month and would run out mid-campaign.

### Gmail sending limits and warm-up

Google Workspace allows about **2,000 messages per user per day**, but **trial accounts are limited to 500 per user per day** (and the survo.tech Workspace trial ends 3 Nov 2026). The code therefore never lets a mailbox send more than **450 a day** (`DAILY_HARD_MAX`), whatever the settings say, leaving room for researchers' replies and the daily report. Hitting Google's limit returns a "daily user sending quota exceeded" / "Retry after" error; the mailbox then stops for the rest of the IST day on its own, and short-term throttling (HTTP 429) only ends the current run.

Each mailbox warms up separately (settings `warmup_*`):

| Stage | Daily cap per mailbox |
|---|---|
| 0 | seed-test day: only seeded test contacts (`warmup_seed_cap`, default 5) |
| 1 → 7 | 20, 40, 60, 100, 150, 200, 250 (`warmup_caps`) |

Once per IST day the Worker looks at the mailbox's most recent **weekday on which it actually sent** that has not been judged yet, and its hard-bounce rate over its **last 3 send days** (`warmup_window_days`):

* below **2 %** (`warmup_max_bounce_rate`) and **no complaints** → up one stage;
* 2 % or more → **hold**;
* **4 %** or more (`warmup_stepback_bounce_rate`) or **any complaint** → **step back** one stage (never below 1).

Days without sends (weekends, blackout, a pause, a mailbox with nothing due) never advance a mailbox. With all five mailboxes clean, total daily capacity goes 100, 200, 300, 500, 750, 1,000, 1,250. The dashboard shows each mailbox's stage, cap, sent today and the last decision with its reason, and **Set stage** lets a researcher hold, step back, or skip the seed day (logged as an admin event).

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
| `warmup_caps` / `warmup_seed_cap` | 20,40,60,100,150,200,250 / 5 | Per-mailbox daily cap at warm-up stage 1…7 / seed messages on the stage-0 day (see warm-up above) |
| `warmup_max_bounce_rate` / `warmup_stepback_bounce_rate` / `warmup_window_days` | 0.02 / 0.04 / 3 | Advance below 2 %, step back at 4 % or any complaint, judged over the mailbox's last 3 send days |
| `cap_max` | 450 | Ceiling over the warm-up caps; the code never allows more than 450/day/mailbox |
| `hourly_cap` | 120 | Per mailbox, over a rolling 60 minutes |
| `per_run_max` | 20 | Per mailbox per 10-minute run. The sender also limits itself to what fits in a 9-minute run at the jitter it uses (about 14). |
| `pace_evenly` | true | Spreads the daily cap across the window instead of sending it all in the morning |
| `reminder_after_workdays` | 5 | One reminder on the 5th workday after the invite. Workdays skip weekends and blackout dates. |
| `reminder2_enabled` / `reminder2_after_workdays` | false / 5 | Optional second reminder |
| `test_mode` / `test_recipients` | **true** / empty | Allow-list only, and the calendar is ignored |
| `complaint_threshold` | 3 | Auto-pause at 3 or more spam complaints. Resuming resets the baseline, so only a new complaint pauses again. |
| `bounce_rate_threshold` / `bounce_window` / `bounce_min_sample` | 0.03 / 200 / 50 | Auto-pause if more than 3% of the last 200 sends hard-bounced, once there are at least 50 sends |
| `heartbeat_max_minutes` | 90 | Auto-pause if no sender run is seen for 90 minutes during an open window while work is due. GitHub cron can lag 15–60 minutes at busy times, so do not set this much lower. |
| `bounce_guard_*` | 4/8/18 and 30/75 | Short-term bounce brake per mailbox (1 h / 24 h / 72 h) and domain-wide (24 h / 72 h); a blocked mailbox resumes when the window rolls over |
| `postal_address`, `from_name`, `reply_to` | empty / India Business Leaders Survey / research@survo.tech | Footer address, From display name (study or researcher name), Reply-To (must be an @survo.tech mailbox so replies are read) |
| `target_end_date` | 2026-12-18 | Used only to compare against the projected finish on the dashboard |

**Order:** for each mailbox, its due reminders always go first, then new invites from the shared queue in `send_order`. `send_order` is a stratified-random interleave over region × size band × nature × email tier, computed at import. The first 80 invites match the full sample's mix to within 2 percentage points on every dimension.

**Who never gets mail:** contacts with status `on_hold`, `bounced`, `opted_out` or `do_not_contact`, and anyone who completed the survey. Anyone who declined consent, complained, or hard-bounced gets no reminders.

**Projection:** with five clean mailboxes, a seed day on 27 Oct and the defaults, the dashboard projection finishes well before the 18 Dec target (see the full fake-mail run below for a simulated campaign). The dashboard recomputes this live from the remaining work and the mailboxes' current stages.

---

## Operating it

### How to pause

* **Dashboard:** click **Pause sending** and enter a reason (required). This takes effect on the next sender run, within 10 minutes. The current run finishes the message it is on. Click **Resume** to continue; the stop-rule counters restart from that moment.
* **Emergency without the dashboard:** disable the `send` workflow (Actions → send → ⋯ → Disable workflow). Or run `npx wrangler d1 execute ibls --remote --command "UPDATE settings SET value='true' WHERE key='paused'"`.
* **Auto-pauses** show as a red banner with their reason: complaints, bounce rate, or missing heartbeat. A mailbox that hits Google's daily sending limit stops for the rest of the IST day on its own.
* **One mailbox only:** on the dashboard, **Set stage** for that mailbox to a lower stage (it then sends less) — there is no per-mailbox pause; to stop one mailbox completely, remove it from `MAILBOXES` (Worker var and GitHub variable).

### How to add contacts

Put the new contacts in a CSV with columns `email,company` and optionally `rid,salutation,director_name,cin,state,region,size_band,nature,email_tier,peer_tier,status,notes`. Then run:

```
python3 scripts/import_contacts.py --csv data/new_contacts.csv --order-offset 100000 --apply --remote
```

RIDs are generated when missing. The script refuses to run on duplicate emails or RIDs. `--order-offset` queues the new contacts after the current queue. Existing contacts are only updated in their descriptive fields, never in their progress. To put one contact on hold or mark it do-not-contact, use the dashboard (search → contact → actions).

### Runbook

| Situation | What happens automatically | What you do |
|---|---|---|
| **Hard bounce** (DSN 5.x.x from Gmail's mailer-daemon or a remote server, or the recipient refused at send time) | Contact → `bounced`, no further mail. Counts toward the bounce-rate stop rule, the bounce guard and that mailbox's warm-up. | Nothing for a single bounce. If the dashboard shows a high rate for one send day or stratum, pause and check that slice of the list. If a better address exists, add it as a new contact. |
| **Soft bounce** (4.x.x, mailbox full) | Recorded. After 3 soft bounces the contact becomes `bounced`. A soft-bounced contact still gets its reminder. | Nothing usually. |
| **Spam complaint** (ARF) | Contact → `do_not_contact`. That mailbox steps back one warm-up stage. **3 complaints pauses everything.** | Read the complaint, check that the templates and footer are intact, and resume only when you are confident. Gmail rarely sends ARF reports to senders: watch the spam rate in Google Postmaster Tools for survo.tech (keep it below 0.3 %). |
| **Opt-out** (link, one-click, or reply "OPT OUT", "unsubscribe", "remove", "stop", "not interested") | Contact → `opted_out`, permanently. The reply shows as informational. | Nothing. |
| **Query / human reply** | Appears under **Replies needing attention** (the mail stays unread in the inbox; replies without our headers are matched through the Gmail thread) | Answer from Gmail (normally research@, the Reply-To), then click **Mark handled**. Wrong person but a referral given: put the contact on hold and add the referred person as a new contact. |
| **Auto-reply / out of office** | Shown as handled. Labelled IBLS/Auto-replies and archived if `FILE_NOISE=1`. | Look occasionally for "has left the company, contact X" replies. |
| **"Bounce guard" reason in health** | The mailbox stops until the rolling window clears | Usually nothing. If it keeps happening, check that slice of the list. |
| **Warm-up hold / step back** | The mailbox stays at, or drops to, a lower daily cap; the reason is in the warm-up table | Usually nothing: it advances again after clean days. |
| **Daily sending limit** (Gmail API "Daily … limit" / "Retry after" a day away; SMTP 5.4.5) | The mailbox stops for the rest of the IST day | Lower that mailbox's stage or `warmup_caps`; on a trial account remember the 500/day limit. |
| **Rate limited** (HTTP 429, short) | The run ends for that mailbox, nothing is recorded against the contacts | Nothing. |
| **Heartbeat auto-pause** | Sending is paused | Check Actions → send. Look for a failed run, an expired secret, or disabled workflows; GitHub disables scheduled workflows in repos with no activity for 60 days. Fix it, then resume. |
| **Auth failure** (`unauthorized_client`, `insufficientPermissions`, `failedPrecondition`) | Job error, that mailbox skipped, others continue | Check the DWD entry (client ID 116272134472583167099 and the three scopes), that the Gmail API is enabled, that the mailbox exists and is licensed, and that `GOOGLE_SA_JSON` is current. In SMTP mode: the app password. |

Mail is **never deleted and never marked read**. The Gmail API reader fetches messages without changing labels; with `FILE_NOISE=1` it only adds an `IBLS/*` label and removes `INBOX` (archive). In IMAP fallback mode reading uses `BODY.PEEK` and filing uses IMAP MOVE.

---

## Data captured

* `/s/RID` logs a click with time, user agent and country. Clicks are tagged `human` or `bot`; link scanners such as Proofpoint, Mimecast and SafeLinks prefetch links, so CTR counts human-looking clicks only. It then redirects to `https://ee.kobotoolbox.org/jbh8Urj6?d[rid]=RID`. An unknown RID redirects without the parameter.
* Kobo stores only submitted forms, so `started` means any submission. `completed` means consent was "1" and the form has an `end` time. Consent "0" is recorded as `consent_declined`. Submissions without a rid are counted as **organic**.
* There is no tracking pixel. Opens are not measured.

## Testing

| Command | What it covers |
|---|---|
| `npx tsc --noEmit` | Worker type check |
| `npx wrangler deploy --dry-run --outdir build/dist` | Bundles the Worker and static assets exactly as a deploy would, without uploading |
| `node --test test/*.test.mjs` | Scheduling unit tests: IST conversion, window, blackout, warm-up caps (never above 450), warm-up decisions (advance / hold / step back / plateau), pacing, workday reminder cutoff, projections (shared queue, five mailboxes, seed day) |
| `python3 -m unittest discover -s tests -t .` | 66 tests: exact template text (fieldwork contact), headers (From, configurable Reply-To, List-Unsubscribe one-click), multipart and no attachments, the send loop (hard and soft failures, quota stop, reconnect, time budget, test-mode guard, dry run, stop if events cannot be recorded); **Gmail transport (mocked HTTP)**: service-account JWT (claims, scopes, signature, cache), raw send, Message-ID read-back, token refresh on 401, error classification (rate / daily quota / invalid recipient / auth / backend), SMTP fallback (smtp.gmail.com:587 STARTTLS, app passwords), fake-transport guard; **rotation** across the five mailboxes; **Gmail DSN classification** (hard, delayed, mailbox full, X-Failed-Recipients, domain not found); **Gmail API sync** (first-run query, history cursor, expired history, thread RID lookup, no cursor advance on failure, labelling, truncation); IMAP fallback sync; Kobo mapping and pagination; the daily report; import ordering and SQL; seed SQL |
| `scripts/local_e2e.sh` (or `make e2e`) | Fresh local D1 → migrations → **import of all 8,092 contacts** → seed → `wrangler dev --local` → the real Python jobs against the Worker (all five mailboxes, fake transport, fake Gmail inbox and Kobo) → 15 HTTP end-to-end tests: static site, auth, click redirect, test-mode isolation and idempotent reservations, live window, start date, warm-up stage caps and pacing, blackout, reminders, opt-out (form and one-click), bounces, replies, complaint auto-pause via the cron trigger and resume, manual pause, hold, dashboard data, CSV export, job API, bounce guard, BIMI logo content type, fieldwork contact on the site, and a per-mailbox warm-up scenario (seed stage, dashboard override, advance, hold, step back, 450 ceiling, Reply-To validation) |
| `scripts/fake_full_run.sh all` (or `make fake-run`) | **Full fake-mail campaign with all contacts**: fresh local D1, one seed per mailbox, test mode off, then every day from 26 Oct with the real sender (five mailboxes in parallel, fake transport) every 10 minutes and the real Gmail-API sync fed with generated Gmail DSNs for ~0.8 % of addresses. Verifies that every contact is invited exactly once, nobody gets two reminders, hard-bounced contacts get no reminder, nothing goes out on weekends or Diwali, no mailbox exceeds 450/day, and the headers are right |
| `scripts/smoke_test.sh` | Read-only checks after a deploy (incl. the BIMI logo) |

`.dev.vars.example` sets `ALLOW_TIME_OVERRIDE=1` so local tests can fake the clock (`?now=`). Never set it in production. The Gmail tests never touch the network: `tests/helpers.py` replaces the HTTP function so any un-mocked Google call fails the test, and `sync_mail.py` refuses `MAIL_TRANSPORT=fake`. The fake run holds a lock so two simulators can never share run ids.
