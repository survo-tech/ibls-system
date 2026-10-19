-- Google Workspace (Gmail) sending: five rotating mailboxes, per-mailbox warm-up, configurable Reply-To.
-- Only fills in new keys and moves values that are still at their old default, so researcher edits survive.

INSERT OR IGNORE INTO settings(key, value) VALUES
  ('warmup_caps', '20,40,60,100,150,200,250'),   -- per-mailbox daily cap at stage 1..7 (stage 0 = seed-test day)
  ('warmup_seed_cap', '5'),                      -- max seed (test) messages per mailbox on the seed-test day
  ('warmup_max_bounce_rate', '0.02'),            -- advance only below 2% hard bounces in the last window
  ('warmup_stepback_bounce_rate', '0.04'),       -- step back one stage at 4% or any complaint
  ('warmup_window_days', '3'),                   -- "last window" = the mailbox's last 3 send days
  ('reply_to', 'research@survo.tech');

-- The fixed ramp is replaced by the per-mailbox warm-up.
DELETE FROM settings WHERE key = 'ramp';

-- cap_max is now only a ceiling over the warm-up caps; the code never allows more than 450/day/mailbox.
UPDATE settings SET value = '450' WHERE key = 'cap_max' AND value = '300';

-- Five mailboxes share one domain: raise the domain-wide bounce guard (per-mailbox limits unchanged).
UPDATE settings SET value = '30' WHERE key = 'bounce_guard_domain_day' AND value = '18';
UPDATE settings SET value = '75' WHERE key = 'bounce_guard_domain_3day' AND value = '45';

-- Invites now come from one shared queue; a queued contact's mailbox is assigned when it is reserved.
-- (Seeded test contacts keep their mailbox so each mailbox gets its own seed test.)
CREATE INDEX IF NOT EXISTS idx_contacts_status_order ON contacts(status, send_order);
CREATE INDEX IF NOT EXISTS idx_contacts_reserved_run ON contacts(reserved_run);
