-- India Business Leaders Survey: initial schema.
-- All timestamps are ISO-8601 UTC strings "YYYY-MM-DDTHH:MM:SS.sssZ" so they sort and compare as text.

CREATE TABLE IF NOT EXISTS contacts (
  rid               TEXT PRIMARY KEY,           -- respondent id, e.g. IB6P2NDL
  email             TEXT NOT NULL UNIQUE,
  salutation        TEXT NOT NULL DEFAULT 'Dear Director,',
  director_name     TEXT,
  company           TEXT NOT NULL,
  cin               TEXT,
  state             TEXT,
  region            TEXT,                       -- North / South / East / West
  size_band         TEXT,                       -- <10cr / 10-100cr / >100cr
  nature            TEXT,                       -- Manufacturing / Non-Manufacturing
  peer_tier         TEXT,                       -- text used in the email, e.g. "over ₹100 crore manufacturing"
  email_tier        TEXT,                       -- TIER_1_CUSTOM_CORPORATE / TIER_2_PUBLIC_CORPORATE
  mailbox           TEXT,                       -- participate@survo.tech / research@survo.tech
  status            TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','on_hold','invited','reminded','completed','bounced','opted_out','do_not_contact')),
  send_order        INTEGER,                    -- stratified-random invite order (lower = earlier)
  is_test           INTEGER NOT NULL DEFAULT 0, -- 1 = seeded test contact (make seed-test)
  invited_at        TEXT,
  invite_msgid      TEXT,
  reminded_at       TEXT,
  reminder_count    INTEGER NOT NULL DEFAULT 0,
  first_click_at    TEXT,
  click_count       INTEGER NOT NULL DEFAULT 0,
  started_at        TEXT,
  completed_at      TEXT,
  consent_declined_at TEXT,
  bounced           TEXT,                       -- NULL / 'soft' / 'hard'
  soft_bounce_count INTEGER NOT NULL DEFAULT 0,
  fail_count        INTEGER NOT NULL DEFAULT 0, -- transient SMTP failures
  complained_at     TEXT,
  opted_out_at      TEXT,
  last_reply_at     TEXT,
  reserved_until    TEXT,                       -- next-batch reservation expiry
  reserved_run      TEXT,                       -- run id that holds the reservation
  reserved_kind     TEXT,                       -- invite / reminder / reminder2
  notes             TEXT,
  updated_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_contacts_queue    ON contacts(mailbox, status, send_order);
CREATE INDEX IF NOT EXISTS idx_contacts_invited  ON contacts(mailbox, status, invited_at);
CREATE INDEX IF NOT EXISTS idx_contacts_reserved ON contacts(reserved_until);
CREATE INDEX IF NOT EXISTS idx_contacts_company  ON contacts(company);

CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  rid        TEXT,
  email      TEXT,
  type       TEXT NOT NULL,   -- sent|failed|bounced_hard|bounced_soft|complaint|reply|auto_reply|opted_out|started|completed|consent_declined|click|admin|released
  kind       TEXT,            -- invite/reminder/reminder2 for sends; smtp for SMTP-time rejects; organic for Kobo w/o rid
  mailbox    TEXT,
  ts         TEXT NOT NULL,
  meta       TEXT,            -- JSON
  test       INTEGER NOT NULL DEFAULT 0, -- 1 = concerns a seeded test contact (excluded from stats/ramp)
  dedupe_key TEXT UNIQUE      -- makes ingestion idempotent (e.g. kobo:123:completed, mail:<mb>:<uidv>:<uid>)
);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts);
CREATE INDEX IF NOT EXISTS idx_events_rid     ON events(rid, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_mb      ON events(type, mailbox, ts);

CREATE TABLE IF NOT EXISTS replies (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  rid            TEXT,
  mailbox        TEXT,
  from_addr      TEXT,
  subject        TEXT,
  snippet        TEXT,
  received_at    TEXT,
  classification TEXT,         -- query / opted_out / auto_reply / complaint
  handled        INTEGER NOT NULL DEFAULT 0,
  handled_by     TEXT,
  handled_at     TEXT,
  message_id     TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_replies_open ON replies(handled, received_at);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  job    TEXT NOT NULL,
  ts     TEXT NOT NULL,
  ok     INTEGER NOT NULL,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_runs ON job_runs(job, ts);

CREATE TABLE IF NOT EXISTS daily_stats (
  day        TEXT PRIMARY KEY,  -- IST calendar date YYYY-MM-DD
  invites    INTEGER NOT NULL DEFAULT 0,
  reminders  INTEGER NOT NULL DEFAULT 0,
  failed     INTEGER NOT NULL DEFAULT 0,
  hard       INTEGER NOT NULL DEFAULT 0,
  soft       INTEGER NOT NULL DEFAULT 0,
  clicks     INTEGER NOT NULL DEFAULT 0,
  started    INTEGER NOT NULL DEFAULT 0,
  completed  INTEGER NOT NULL DEFAULT 0,
  declined   INTEGER NOT NULL DEFAULT 0,
  opted_out  INTEGER NOT NULL DEFAULT 0,
  complaints INTEGER NOT NULL DEFAULT 0,
  replies    INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
);

-- Defaults. The Worker also has code defaults; rows here make them visible/editable.
-- test_mode starts TRUE: nothing reaches real contacts until a researcher turns it off.
INSERT OR IGNORE INTO settings(key, value) VALUES
  ('paused', 'false'),
  ('pause_reason', ''),
  ('test_mode', 'true'),
  ('test_recipients', ''),
  ('start_date', '2026-10-27'),
  ('end_date', ''),
  ('target_end_date', '2026-12-18'),
  ('window_start', '10:00'),
  ('window_end', '17:00'),
  ('send_weekdays', '1,2,3,4,5'),
  ('blackout_dates', '2026-11-05..2026-11-11'),
  ('ramp', '40,70,110,160,220,300'),
  ('cap_max', '300'),
  ('hourly_cap', '120'),
  ('per_run_max', '20'),
  ('pace_evenly', 'true'),
  ('reminder_after_workdays', '5'),
  ('reminder2_enabled', 'false'),
  ('reminder2_after_workdays', '5'),
  ('postal_address', ''),
  ('from_name', 'India Business Leaders Survey'),
  ('complaint_threshold', '3'),
  ('bounce_rate_threshold', '0.03'),
  ('bounce_window', '200'),
  ('bounce_min_sample', '50'),
  ('heartbeat_max_minutes', '90'),
  -- Titan bounce limits (support.titan.email "Email Bounce Back"): mailbox 5/h, 10/day, 20/3 days;
  -- domain 10/h, 20/day, 50/3 days (paid). The guard stops a mailbox just below them.
  ('bounce_guard_mailbox_hour', '4'),
  ('bounce_guard_mailbox_day', '8'),
  ('bounce_guard_mailbox_3day', '18'),
  ('bounce_guard_domain_day', '18'),
  ('bounce_guard_domain_3day', '45'),
  ('stop_rule_reset_at', ''),
  ('stop_rule_complaint_baseline', '0');
