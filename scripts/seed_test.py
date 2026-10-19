#!/usr/bin/env python3
"""
Seed (or reset) test contacts for an end-to-end dry run with real email.

  python3 scripts/seed_test.py --emails you@example.com,colleague@example.org [--apply --remote|--local]

* Creates contacts TEST0001.. with is_test=1, status queued, alternating mailboxes, a fake company name.
* Re-running resets them to queued (clears invited/reminded/click/bounce state) so you can test again.
* Sets the `test_recipients` setting to these emails and FORCES test_mode=true.
Test contacts are excluded from all dashboard statistics and from the ramp-day count.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

MAILBOXES = ["participate@survo.tech", "research@survo.tech"]


def q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def build_sql(emails: list[str]) -> str:
    lines = []
    for i, e in enumerate(emails, start=1):
        rid = f"TEST{i:04d}"
        lines.append(
            "INSERT INTO contacts (rid, email, salutation, company, region, size_band, nature, peer_tier, email_tier, mailbox, status, send_order, is_test)"
            f" VALUES ({q(rid)}, {q(e)}, 'Dear Tester,', {q(f'Test Company {i} Private Limited')}, 'North', '10-100cr', 'Manufacturing',"
            f" '₹10–100 crore manufacturing', 'TEST', {q(MAILBOXES[(i - 1) % 2])}, 'queued', {-1000 + i}, 1)"
            " ON CONFLICT(rid) DO UPDATE SET email=excluded.email, mailbox=excluded.mailbox, status='queued', is_test=1,"
            " invited_at=NULL, invite_msgid=NULL, reminded_at=NULL, reminder_count=0, first_click_at=NULL, click_count=0,"
            " started_at=NULL, completed_at=NULL, consent_declined_at=NULL, bounced=NULL, soft_bounce_count=0, fail_count=0,"
            " complained_at=NULL, opted_out_at=NULL, last_reply_at=NULL, reserved_until=NULL, reserved_run=NULL, reserved_kind=NULL;"
        )
    lines.append(f"INSERT INTO settings(key, value) VALUES ('test_recipients', {q(','.join(emails))}) ON CONFLICT(key) DO UPDATE SET value=excluded.value;")
    lines.append("INSERT INTO settings(key, value) VALUES ('test_mode', 'true') ON CONFLICT(key) DO UPDATE SET value='true';")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emails", required=True, help="comma-separated allowlist")
    ap.add_argument("--out", default="build/seed_test.sql")
    ap.add_argument("--db", default="ibls")
    ap.add_argument("--apply", action="store_true")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--remote", action="store_true")
    g.add_argument("--local", action="store_true")
    a = ap.parse_args(argv)
    emails = [e.strip().lower() for e in re.split(r"[\s,;]+", a.emails) if e.strip()]
    bad = [e for e in emails if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e)]
    if not emails or bad:
        print(f"bad emails: {bad or 'none given'}", file=sys.stderr)
        return 2
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_sql(emails), encoding="utf-8")
    print(f"wrote {out} for {len(emails)} test recipient(s)")
    if a.apply:
        if not (a.remote or a.local):
            print("--apply needs --remote or --local", file=sys.stderr)
            return 2
        subprocess.run(["npx", "wrangler", "d1", "execute", a.db, "--remote" if a.remote else "--local", "--file", str(out), "-y"], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
