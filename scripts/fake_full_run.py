#!/usr/bin/env python3
"""
Full fake-mail campaign against a LOCAL Worker (wrangler dev --local with ALLOW_TIME_OVERRIDE=1): every
contact, every send day, the real sender (all five mailboxes in parallel) with FakeTransport, and the real
Gmail-API sync job fed with generated Gmail DSNs for a deterministic ~0.8% of recipients. Nothing is sent.

  python3 scripts/fake_full_run.py --base http://127.0.0.1:8789 --init            # test mode off (after seeding)
  python3 scripts/fake_full_run.py --base http://127.0.0.1:8789 --from 2026-10-26 --days 5
  python3 scripts/fake_full_run.py --base http://127.0.0.1:8789 --verify

Runs are resumable (the Worker's D1 holds the campaign state; the sent log is build/fake-run/sent.jsonl), so a
long campaign can be simulated in chunks. scripts/fake_full_run.sh drives the whole thing.
"""
from __future__ import annotations

import argparse
import base64
import collections
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jobs"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("API_TOKEN", "local-test-token-0123456789")

import send  # noqa: E402
import sync_mail  # noqa: E402
from common import Api  # noqa: E402
from transport import FakeTransport  # noqa: E402
from tests.test_gmail import GMAIL_DSN, FakeGmailClient  # noqa: E402

MAILBOXES = ["participate@survo.tech", "research@survo.tech", "survey@survo.tech", "study@survo.tech", "team@survo.tech"]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
OUT = ROOT / "build" / "fake-run"
BLACKOUT = {f"2026-11-{d:02d}" for d in range(5, 12)}
BOUNCE_PER_10000 = 80  # ~0.8% of recipients hard-bounce (deterministic by address)


def bounces(addr: str) -> bool:
    return int(hashlib.sha256(addr.encode()).hexdigest()[:8], 16) % 10000 < BOUNCE_PER_10000


def dash(base, path, body=None):
    req = urllib.request.Request(base + "/dashboard/api/" + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode(),
                                          "x-ibls-dashboard": "1", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def record(day: dt.date, mb: str, m) -> dict:
    return {"day": str(day), "mailbox": mb, "rid": m["X-IBLS-RID"], "kind": m["X-IBLS-Kind"], "to": m["To"], "from": m["From"],
            "reply_to": m["Reply-To"], "lu": bool(m["List-Unsubscribe"]), "lup": m["List-Unsubscribe-Post"]}


def run_day(base: str, api: Api, day: dt.date, log_f, deadline: float = float("inf")) -> dict:
    """One IST day: 42 sender runs (every 10 min, 10:00-16:50) x 5 mailboxes, then that day's DSNs, then cron.
    The sent log is written after every run, so a day interrupted half-way can simply be run again."""
    stats = collections.Counter()
    for slot in range(42):
        if time.monotonic() > deadline:
            return {"day": str(day), "interrupted": True}  # stop between runs; re-running the day resumes it
        t = dt.datetime(day.year, day.month, day.day, 10, 0, tzinfo=IST) + dt.timedelta(minutes=10 * slot, seconds=30)
        sink: list = []

        def one(mb, t=t, sink=sink):
            s = send.MailboxSender(mb, api, f"fake-{t:%Y%m%d%H%M}", jitter=(0, 0), transport=FakeTransport(mb, sink=sink),
                                   sleep=lambda x: None, now_fn=lambda: t)
            return s.run()
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            res = list(ex.map(one, MAILBOXES))
        for mb, m in sink:
            log_f.write(json.dumps(record(day, mb, m)) + "\n")
        log_f.flush()
        for r in res:
            stats["sent"] += r["sent"]
            stats["errors"] += bool(r.get("error"))
        if slot == 0 and not any(r["sent"] for r in res) and \
                all(any(w in r["reason"] for w in ("weekday", "blackout", "before start")) for r in res):
            return {"day": str(day), "closed": res[0]["reason"]}
        # Speed-up only: once every mailbox has used its whole cap or has nothing due, later runs today cannot send.
        def done(r):
            c = r.get("caps") or {}
            return r["reason"] == "nothing due" or "no seed contacts left" in r["reason"] or \
                (c and c.get("sent_today", 0) >= c.get("daily_cap", 1) and r["sent"] == 0)
        if all(done(r) for r in res):
            break
    # DSNs for today's invites to addresses that "bounce", delivered through the real Gmail-API sync job.
    rows = [json.loads(x) for x in (OUT / "sent.jsonl").read_text().splitlines() if x.strip()]
    today = [r for r in rows if r["day"] == str(day)]
    dsns = collections.defaultdict(dict)
    for r in today:
        if r["kind"] == "invite" and not r["rid"].startswith("TEST") and bounces(r["to"]):
            raw = GMAIL_DSN.replace(b"IBDSNG55", r["rid"].encode()).replace(b"nobody@acme.example", r["to"].encode()) \
                .replace(b"survey@survo.tech", r["mailbox"].encode())
            ts = int(dt.datetime(day.year, day.month, day.day, 17, 30, tzinfo=IST).timestamp() * 1000)
            dsns[r["mailbox"]][f"dsn-{r['rid']}"] = (raw, ["INBOX", "UNREAD"], ts, f"t-{r['rid']}")
    for mb, msgs in dsns.items():
        # The mailbox's history lists the new DSNs (the first sync of a mailbox uses the query path instead).
        hist = [{"id": day.strftime("%Y%m%d") + "0", "messagesAdded": [{"message": {"id": mid, "labelIds": ["INBOX", "UNREAD"]}} for mid in msgs]}]
        st = sync_mail.sync_mailbox_gmail(api, mb, client=FakeGmailClient(mb, msgs, history_id=day.strftime("%Y%m%d") + "9", history=hist),
                                          file_noise=False)
        if st["posted"] + st.get("duplicate", 0) != len(msgs):
            raise SystemExit(f"DSN sync for {mb} on {day} posted {st['posted']} of {len(msgs)}")
        stats["dsn_posted"] += st["posted"]
    urllib.request.urlopen(base + "/__scheduled?cron=*/15+*+*+*+*", timeout=60).read()  # stop rules + rollups
    by_mb = collections.Counter(r["mailbox"] for r in today)
    return {"day": str(day), "sent_today": len(today), "by_mailbox": dict(by_mb), "dsn": stats["dsn_posted"], "errors": stats["errors"]}


def verify(base: str) -> int:
    rows = [json.loads(x) for x in (OUT / "sent.jsonl").read_text().splitlines() if x.strip()]
    problems = []
    inv = collections.Counter(r["rid"] for r in rows if r["kind"] == "invite")
    rem = collections.Counter(r["rid"] for r in rows if r["kind"] == "reminder")
    per_day_mb = collections.Counter((r["day"], r["mailbox"]) for r in rows)
    real = [r for r in rows if not r["rid"].startswith("TEST")]
    if any(n > 1 for n in inv.values()):
        problems.append(f"{sum(n > 1 for n in inv.values())} contacts invited more than once")
    if any(n > 1 for n in rem.values()):
        problems.append(f"{sum(n > 1 for n in rem.values())} contacts got the same reminder twice")
    if any(r["kind"] == "reminder2" for r in rows):
        problems.append("a second reminder went out although reminder2 is disabled")
    for (day, mb), n in per_day_mb.items():
        if n > 450:
            problems.append(f"{mb} sent {n} on {day} (> 450)")
        d = dt.date.fromisoformat(day)
        if d.weekday() >= 5 or day in BLACKOUT:
            problems.append(f"sent on non-send day {day}")
    for r in rows:
        if r["from"] != f"India Business Leaders Survey <{r['mailbox']}>" or r["reply_to"] != "research@survo.tech" or not r["lu"] \
                or r["lup"] != "List-Unsubscribe=One-Click":
            problems.append(f"bad headers {r}")
            break
    # remind check: reminders only for invited, never for bounced
    bounced = {r["rid"] for r in real if r["kind"] == "invite" and bounces(r["to"])}
    if bounced & set(rem):
        problems.append(f"{len(bounced & set(rem))} hard-bounced contacts got a reminder")
    s = dash(base, "summary")
    k = s["kpis"]
    mbs = s["schedule"]["mailboxes"]
    print(json.dumps({
        "messages": len(rows), "real_invites": sum(1 for r in real if r["kind"] == "invite"), "reminders": sum(1 for r in real if r["kind"] == "reminder"),
        "seed_messages": len(rows) - len(real), "send_days": len({r['day'] for r in real}), "first_day": min(r["day"] for r in real),
        "last_invite_day": max(r["day"] for r in real if r["kind"] == "invite"), "last_day": max(r["day"] for r in real),
        "max_per_mailbox_day": max(per_day_mb.values()),
        "kpis": {x: k[x] for x in ("contacts", "sendable", "queued", "invited", "reminded", "bounced_hard", "on_hold")},
        "stages": {m["mailbox"]: m["stage"] for m in mbs},
        "warmup_decisions": {m["mailbox"]: collections.Counter(h["action"] for h in m.get("history", [])) for m in dash(base, "mailboxes")["mailboxes"]},
        "problems": problems}, indent=1, default=dict))
    if k["queued"] != 0:
        problems.append(f"{k['queued']} contacts never invited")
    if k["invited"] != len({r["rid"] for r in real if r["kind"] == "invite"}):
        problems.append("invited count mismatch between the Worker and the sent log")
    if s["health"]["paused"]:
        problems.append("campaign ended paused: " + str(s["health"]["pause_reason"]))
    for p in problems:
        print("PROBLEM:", p)
    return 1 if problems else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8789")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--budget", type=float, default=0, help="stop cleanly after this many seconds (0 = no limit)")
    a = ap.parse_args(argv)
    if not a.base.startswith(("http://127.0.0.1", "http://localhost")):
        raise SystemExit("local Worker only")
    os.environ["API_BASE"] = a.base
    OUT.mkdir(parents=True, exist_ok=True)
    if a.init:
        (OUT / "sent.jsonl").write_text("")
        print(dash(a.base, "settings", {"test_mode": "false"}))
        return 0
    if a.verify:
        return verify(a.base)
    api = Api(base=a.base)
    d = dt.date.fromisoformat(a.start)
    with open(OUT / "sent.jsonl", "a") as f:
        deadline = time.monotonic() + a.budget if a.budget else float("inf")
        for i in range(a.days):
            r = run_day(a.base, api, d + dt.timedelta(days=i), f, deadline)
            f.flush()
            print(json.dumps(r), flush=True)
            if r.get("interrupted"):
                print(f"RESUME {r['day']}", flush=True)
                break
        else:
            print(f"RESUME {(d + dt.timedelta(days=a.days)).isoformat()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
