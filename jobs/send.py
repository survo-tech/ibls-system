#!/usr/bin/env python3
"""
Sender job (GitHub Actions, every 10 min in the IST send window).

For each mailbox, in parallel:
  1. GET /api/next-batch?mailbox=..&max=..&run_id=..  (the Worker applies pause, test mode, window, caps, ordering)
  2. render each message, send it individually over Titan SMTP (SSL 465) with 20–60 s random jitter
  3. POST the outcome (sent / failed / bounced_hard) right after each message
  4. stop the mailbox on quota/rate errors (5.4.x, 4.7.x, "quota"), auth errors or repeated failures
  5. release any reservation it could not use, then POST a heartbeat to /api/job-run

Environment
  API_BASE (default https://survo.tech), API_TOKEN, SMTP_PASS_PARTICIPATE, SMTP_PASS_RESEARCH
  SMTP_HOST (smtp.titan.email), SMTP_PORT (465), IMAP_HOST (imap.titan.email) for SAVE_SENT
  JITTER_MIN (20), JITTER_MAX (60), RUN_BUDGET_SEC (540), MAX_PER_RUN (20), SAVE_SENT (0), SENT_FOLDER (Sent)
  DRY_RUN (0): render + log only, release reservations, send nothing
"""
from __future__ import annotations

import concurrent.futures
import imaplib
import os
import random
import re
import secrets
import smtplib
import socket
import sys
import time
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import formataddr, formatdate

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import MAILBOXES, Api, env, iso, log, mailbox_password  # noqa: E402
from render import render  # noqa: E402

DOMAIN = "survo.tech"
REPLY_TO = "participate@survo.tech"
QUOTA_RE = re.compile(r"\b(5\.4\.\d+|4\.7\.\d+)\b|quota|limit exceeded|rate limit|too many (messages|recipients|connections)|sending limit", re.I)


def make_msgid(rid: str, kind: str) -> str:
    """Message-ID that encodes the RID so replies (In-Reply-To/References) can be matched back."""
    return f"<ibls.{rid}.{kind}.{secrets.token_hex(6)}@{DOMAIN}>"


def build_message(m: dict, settings: dict, mailbox: str) -> EmailMessage:
    ctx = {k: m[k] for k in ("salutation", "company", "peer_tier", "survey_link", "optout_link")}
    ctx["postal_address"] = settings.get("postal_address") or ""
    subject, text, html = render(m["kind"], ctx)
    msg = EmailMessage(policy=SMTP_POLICY)
    msg["From"] = formataddr((settings.get("from_name") or "India Business Leaders Survey", mailbox))
    msg["To"] = m["email"]
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(m["rid"], m["kind"])
    msg["Reply-To"] = REPLY_TO
    msg["List-Unsubscribe"] = f"<mailto:{REPLY_TO}?subject=OPT%20OUT>, <{m['optout_link']}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["X-IBLS-RID"] = m["rid"]
    msg["X-IBLS-Kind"] = m["kind"]
    if m["kind"] != "invite" and m.get("invite_msgid"):
        msg["In-Reply-To"] = m["invite_msgid"]
        msg["References"] = m["invite_msgid"]
    msg.set_content(text, charset="utf-8")
    msg.add_alternative(html, subtype="html", charset="utf-8")
    return msg


def classify_smtp_error(exc: Exception) -> tuple[str, int | None, str]:
    """-> (category, code, text). Categories: hard, soft, quota, auth, conn."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "auth", exc.smtp_code, _txt(exc.smtp_error)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        code, err = next(iter(exc.recipients.values()))
        text = _txt(err)
        if QUOTA_RE.search(text):
            return "quota", code, text
        return ("hard" if 500 <= code < 600 else "soft"), code, text
    if isinstance(exc, (smtplib.SMTPSenderRefused, smtplib.SMTPDataError, smtplib.SMTPResponseException)):
        code, text = exc.smtp_code, _txt(exc.smtp_error)
        if QUOTA_RE.search(text) or code == 421:
            return "quota", code, text
        return "soft", code, text  # message-level rejection: count as failed, do not mark the address bad
    if isinstance(exc, (smtplib.SMTPServerDisconnected, socket.error, TimeoutError, ConnectionError)):
        return "conn", None, str(exc)
    return "soft", None, repr(exc)


def _txt(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)


class MailboxSender:
    def __init__(self, mailbox: str, api: Api, run_id: str, *, dry_run=False, budget=540.0, jitter=(20.0, 60.0),
                 max_per_run=20, save_sent=False, smtp_factory=None, imap_factory=None, sleep=time.sleep, clock=time.monotonic):
        self.mailbox, self.api, self.run_id = mailbox, api, run_id
        self.dry_run, self.budget, self.jitter = dry_run, budget, jitter
        self.max_per_run, self.save_sent = max_per_run, save_sent
        self.smtp_factory = smtp_factory or (lambda: smtplib.SMTP_SSL(env("SMTP_HOST", "smtp.titan.email"), int(env("SMTP_PORT", "465")), timeout=60))
        self.imap_factory = imap_factory or (lambda: imaplib.IMAP4_SSL(env("IMAP_HOST", "imap.titan.email"), 993))
        self.sleep, self.clock = sleep, clock
        self.smtp = None
        self.imap = None
        self.stats = {"mailbox": mailbox, "sent": 0, "failed": 0, "hard": 0, "skipped": 0, "reason": "", "quota": False, "error": ""}

    # --- connections -------------------------------------------------------
    def _connect(self):
        s = self.smtp_factory()
        s.login(self.mailbox, mailbox_password(self.mailbox))
        self.smtp = s

    def _close(self):
        for c, quit_ in ((self.smtp, "quit"), (self.imap, "logout")):
            if c is not None:
                try:
                    getattr(c, quit_)()
                except Exception:
                    pass
        self.smtp = self.imap = None

    def _append_sent(self, msg: EmailMessage):
        try:
            if self.imap is None:
                self.imap = self.imap_factory()
                self.imap.login(self.mailbox, mailbox_password(self.mailbox))
            self.imap.append(env("SENT_FOLDER", "Sent"), "\\Seen", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        except Exception as e:  # optional feature: never fail a send because of it
            log.warning("[%s] IMAP APPEND to Sent failed: %s", self.mailbox, e)

    # --- main loop -----------------------------------------------------------
    def planned_max(self) -> int:
        avg = (self.jitter[0] + self.jitter[1]) / 2 or 1
        return max(0, min(self.max_per_run, int(self.budget // avg) + 1))

    def run(self) -> dict:
        start = self.clock()
        want = self.planned_max()
        batch = self.api.get("/api/next-batch", mailbox=self.mailbox, max=want, run_id=self.run_id)
        msgs = batch.get("messages", [])
        settings = batch.get("settings", {})
        self.stats["reason"] = batch.get("reason", "") or ""
        self.stats["caps"] = batch.get("caps")
        if not msgs:
            log.info("[%s] nothing to send: %s", self.mailbox, self.stats["reason"])
            return self.stats
        run_key = batch.get("run_id", self.run_id)
        allow = set(e.lower() for e in settings.get("test_recipients", []))
        if settings.get("test_mode") and any(m["email"].lower() not in allow for m in msgs):
            # Defence in depth: the Worker should never do this, but never risk it.
            self.stats["error"] = "test_mode on but batch contains non-test recipients; aborting"
            log.error("[%s] %s", self.mailbox, self.stats["error"])
            self.api.post("/api/release", {"run_id": run_key})
            return self.stats
        log.info("[%s] %d message(s) reserved (test_mode=%s)", self.mailbox, len(msgs), settings.get("test_mode"))
        consecutive_fail = 0
        try:
            for i, m in enumerate(msgs):
                if i:
                    pause = random.uniform(*self.jitter)
                    if self.clock() - start + pause > self.budget:
                        self.stats["reason"] = "run time budget reached"
                        break
                    self.sleep(pause)
                msg = build_message(m, settings, self.mailbox)
                if self.dry_run:
                    log.info("[%s] DRY RUN %s -> %s: %s", self.mailbox, m["kind"], m["email"], msg["Subject"])
                    self.stats["skipped"] += 1
                    continue
                outcome = self._send_one(m, msg)
                if outcome in ("quota", "auth", "posting"):
                    break
                consecutive_fail = consecutive_fail + 1 if outcome == "failed" else 0
                if consecutive_fail >= 3:
                    self.stats["error"] = "3 consecutive failures; stopping this mailbox for this run"
                    break
        finally:
            self._close()
            try:
                self.api.post("/api/release", {"run_id": run_key})
            except Exception as e:
                log.warning("[%s] release failed (reservations expire in 30 min anyway): %s", self.mailbox, e)
        return self.stats

    def _send_one(self, m: dict, msg: EmailMessage) -> str:
        meta = {"message_id": msg["Message-ID"], "subject": str(msg["Subject"])}
        for attempt in (1, 2):
            try:
                if self.smtp is None:
                    self._connect()
                self.smtp.send_message(msg, from_addr=self.mailbox, to_addrs=[m["email"]])
                break
            except Exception as e:  # noqa: BLE001 - classify everything SMTP can throw
                cat, code, text = classify_smtp_error(e)
                if cat == "conn" and attempt == 1:
                    log.warning("[%s] connection problem (%s); reconnecting", self.mailbox, text)
                    self._close()
                    continue
                log.warning("[%s] %s -> %s: %s %s", self.mailbox, m["rid"], cat, code, text[:200])
                ev = {"rid": m["rid"], "email": m["email"], "mailbox": self.mailbox, "kind": m["kind"], "ts": iso(),
                      "meta": {**meta, "code": code, "error": text[:300]}}
                if cat == "hard":
                    ev.update(type="bounced_hard", kind="smtp", id=f"smtp-reject:{m['rid']}:{msg['Message-ID']}")
                    ev["meta"]["stage"] = "smtp"
                    self.stats["hard"] += 1
                else:
                    ev.update(type="failed")
                    self.stats["failed"] += 1
                self._post([ev])
                if cat == "quota":
                    self.stats["quota"] = True
                    self.stats["reason"] = f"SMTP quota/rate limit: {code} {text[:120]}"
                    return "quota"
                if cat == "auth":
                    self.stats["error"] = f"SMTP authentication failed for {self.mailbox}"
                    return "auth"
                return "hard" if cat == "hard" else "failed"
        else:
            return "failed"
        self.stats["sent"] += 1
        if self.save_sent:
            self._append_sent(msg)
        ok = self._post([{"rid": m["rid"], "email": m["email"], "type": "sent", "kind": m["kind"], "mailbox": self.mailbox,
                          "ts": iso(), "meta": meta, "id": f"sent:{msg['Message-ID']}"}])
        return "sent" if ok else "posting"

    def _post(self, events: list[dict]) -> bool:
        try:
            self.api.events(events)
            return True
        except Exception as e:
            # Stop sending: an unrecorded send would be re-sent after the reservation expires.
            self.stats["error"] = f"could not record events ({e}); stopped to avoid duplicates. Unrecorded: {events}"
            log.error("[%s] %s", self.mailbox, self.stats["error"])
            return False


def main() -> int:
    api = Api()
    run_id = f"gh-{env('GITHUB_RUN_ID', 'local')}-{env('GITHUB_RUN_ATTEMPT', '1')}-{secrets.token_hex(3)}"
    opts = dict(
        dry_run=env("DRY_RUN", "0") == "1",
        budget=float(env("RUN_BUDGET_SEC", "540")),
        jitter=(float(env("JITTER_MIN", "20")), float(env("JITTER_MAX", "60"))),
        max_per_run=int(env("MAX_PER_RUN", "20")),
        save_sent=env("SAVE_SENT", "0") == "1",
    )
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(MAILBOXES)) as ex:
        futs = {ex.submit(MailboxSender(mb, api, run_id, **opts).run): mb for mb in MAILBOXES}
        for f in concurrent.futures.as_completed(futs):
            mb = futs[f]
            try:
                results.append(f.result())
            except Exception as e:  # noqa: BLE001
                log.exception("[%s] crashed", mb)
                results.append({"mailbox": mb, "error": f"crash: {e}"})
    ok = not any(r.get("error") for r in results)
    detail = {"run_id": run_id, "results": [{k: v for k, v in r.items() if k != "caps"} for r in results]}
    for r in results:
        if r.get("quota"):
            api.job_run("send", False, f"quota hit on {r['mailbox']}: {r.get('reason')}", quota_mailbox=r["mailbox"])
    api.job_run("send", ok, detail)
    log.info("done: %s", detail)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
