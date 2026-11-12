-- Finish the campaign faster: every mailbox sends the top warm-up cap (250/day),
-- and the send window matches the sender (06:00–22:00 IST). Stop rules and the
-- 450/day hard ceiling are unchanged. A researcher can still edit these on the dashboard.

UPDATE settings SET value = '250,250,250,250,250,250,250' WHERE key = 'warmup_caps';
UPDATE settings SET value = '06:00' WHERE key = 'window_start';
UPDATE settings SET value = '22:00' WHERE key = 'window_end';

-- Pin each mailbox that has already warmed up at stage 7, and treat 12 Nov 2026 as
-- already judged, so the next morning does not immediately overwrite this override.
UPDATE settings
SET value = json_set(
  value,
  '$.stage', 7,
  '$.evaluated_through',
    CASE
      WHEN COALESCE(json_extract(value, '$.evaluated_through'), '') > '2026-11-12'
      THEN json_extract(value, '$.evaluated_through')
      ELSE '2026-11-12'
    END,
  '$.last', json_object(
    'day', '2026-11-13',
    'action', 'manual',
    'from', COALESCE(json_extract(value, '$.stage'), 0),
    'to', 7,
    'reason', 'set every mailbox to 250/day to finish the campaign faster',
    'at', '2026-11-12T20:45:00.000Z'
  )
)
WHERE key IN (
  'state:warmup:participate@survo.tech',
  'state:warmup:research@survo.tech',
  'state:warmup:survey@survo.tech',
  'state:warmup:study@survo.tech',
  'state:warmup:team@survo.tech'
);
