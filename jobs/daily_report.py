#!/usr/bin/env python3
"""
Daily summary email (GitHub Actions, 12:30 UTC = 18:00 IST) to REPORT_TO (comma list) from research@survo.tech,
sent through the same transport as the invitations (Gmail API, or smtp.gmail.com with MAIL_TRANSPORT=smtp).

Environment: API_BASE, API_TOKEN, REPORT_TO, REPORT_FROM (research@survo.tech), MAIL_TRANSPORT,
GOOGLE_SA_JSON | GMAIL_APP_PW_RESEARCH, DASHBOARD_URL (https://survo.tech/dashboard), DRY_RUN (print only)
"""
from __future__ import annotations

import os
import sys
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Api, env, log  # noqa: E402
from transport import make_transport  # noqa: E402


def pct(n, d) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "–"


def compose(s: dict, dashboard_url: str) -> tuple[str, str]:
    t, k = s["today"], s["totals"]
    status = "PAUSED: " + (s.get("pause_reason") or "") if s.get("paused") else ("TEST MODE" if s.get("test_mode") else "live")
    subject = f"IBLS daily {s['day']}: {t['invites']} invites, {t['reminders']} reminders, {t['completed']} completes" + (" [PAUSED]" if s.get("paused") else "")
    lines = [
        f"India Business Leaders Survey: daily summary for {s['day']} (IST)",
        f"Sending status: {status}",
        "",
        "Today",
        f"  Invitations sent   {t['invites']}",
        f"  Reminders sent     {t['reminders']}",
        f"  Failed attempts    {t['failed']}",
        f"  Hard / soft bounce {t['hard']} / {t['soft']}",
        f"  Link clicks        {t['clicks']}",
        f"  Surveys completed  {t['completed']}  (declined consent: {t['declined']})",
        f"  Opt-outs           {t['opted_out']}",
        f"  Spam complaints    {t['complaints']}",
        "",
        "Cumulative",
        f"  Invited            {k.get('invited', 0)} of {k.get('sendable', 0)} sendable",
        f"  Completed          {k.get('completed', 0)} ({pct(k.get('completed', 0), k.get('invited', 0))} of invited)",
        f"  Hard bounces       {k.get('bounced_hard', 0)} ({pct(k.get('bounced_hard', 0), k.get('invited', 0))})",
        f"  Opt-outs           {k.get('opted_out', 0)}",
        f"  Complaints         {k.get('complaints', 0)}",
        f"  Replies needing attention: {k.get('replies_open', 0)}",
        "",
    ]
    if s.get("mailboxes"):
        lines.append("Mailboxes (sent today / cap, warm-up stage)")
        for m in s["mailboxes"]:
            last = m.get("last") or {}
            note = f"  last: {last.get('action')} ({last.get('reason')})" if last else ""
            lines.append(f"  {m['mailbox']:<24} {m.get('sent_today', 0):>4} / {m.get('daily_cap', 0):<4} {m.get('stage_label', '')}{note}")
        lines.append("")
    lines.append("Jobs (last run, UTC)")
    for j in s.get("jobs", []):
        lines.append(f"  {j['job']:<14} {j['ts'][:16].replace('T', ' ')}  {'ok' if j['ok'] else 'ERROR'}")
    lines += ["", f"Dashboard: {dashboard_url}"]
    return subject, "\n".join(lines) + "\n"


def main(api=None, transport=None) -> int:
    api = api or Api()
    to = [a.strip() for a in env("REPORT_TO", required=True).split(",") if a.strip()]
    sender = env("REPORT_FROM", "research@survo.tech")
    try:
        s = api.get("/api/summary")
        subject, body = compose(s, env("DASHBOARD_URL", "https://survo.tech/dashboard"))
        msg = EmailMessage()
        msg["From"] = formataddr(("IBLS outreach system", sender))
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(domain="survo.tech")
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(body)
        if env("DRY_RUN", "0") == "1":
            print(msg)
        else:
            t = transport or make_transport(sender)
            try:
                t.send(msg, to)
            finally:
                t.close()
        api.job_run("daily_report", True, f"sent to {len(to)} recipient(s)")
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("daily report failed")
        api.job_run("daily_report", False, str(e)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())
