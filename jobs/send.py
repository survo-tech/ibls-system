#!/usr/bin/env python3
"""
Sender job (GitHub Actions, every 10 min in the IST send window).

For each of the five mailboxes, in parallel:
  1. GET /api/next-batch?mailbox=..&max=..&run_id=..  (the Worker applies pause, test mode, window, blackout,
     the mailbox's warm-up cap, hourly cap and ordering; invites come from one shared queue)
  2. render each message and send it individually with 20–60 s random jitter, through the Gmail API
     (service account + domain-wide delegation) or, with MAIL_TRANSPORT=smtp, smtp.gmail.com:587 STARTTLS
  3. POST the outcome (sent / failed / bounced_hard) right after each message
  4. stop the mailbox on quota/rate errors, auth errors or repeated failures
  5. release any reservation it could not use, then POST a heartbeat to /api/job-run

Environment
  API_BASE (default https://survo.tech), API_TOKEN, MAILBOXES (the five @survo.tech mailboxes)
  MAIL_TRANSPORT (gmail_api | smtp), GOOGLE_SA_JSON (gmail_api), GMAIL_APP_PW_<LOCALPART> (smtp)
  JITTER_MIN (20), JITTER_MAX (60), RUN_BUDGET_SEC (540), MAX_PER_RUN (20), REPLY_TO (fallback if the Worker sends none)
  DRY_RUN (0): render + log only, release reservations, send nothing
"""
from __future__ import annotations

import concurrent.futures
import os
import random
import secrets
import sys
import time
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import formataddr, formatdate

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_REPLY_TO, MAILBOXES, Api, env, iso, log  # noqa: E402
from render import render  # noqa: E402
from transport import (FakeTransport, SmtpTransport, TransportError, classify_smtp_error,  # noqa: E402,F401
                       make_transport)

DOMAIN = "survo.tech"
DEFAULT_FROM_NAME = "India Business Leaders Survey"


def make_msgid(rid: str, kind: str) -> str:
    """Message-ID that encodes the RID so replies (In-Reply-To/References) can be matched back."""
    return f"<ibls.{rid}.{kind}.{secrets.token_hex(6)}@{DOMAIN}>"


def build_message(m: dict, settings: dict, mailbox: str) -> EmailMessage:
    ctx = {k: m[k] for k in ("salutation", "company", "peer_tier", "survey_link", "optout_link")}
    ctx["postal_address"] = settings.get("postal_address") or ""
    subject, text, html = render(m["kind"], ctx)
    reply_to = (settings.get("reply_to") or env("REPLY_TO", DEFAULT_REPLY_TO)).strip()
    msg = EmailMessage(policy=SMTP_POLICY)
    # Display name: the study (or a researcher's name, set on the dashboard); address: the sending mailbox.
    msg["From"] = formataddr((settings.get("from_name") or DEFAULT_FROM_NAME, mailbox))
    msg["To"] = m["email"]
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(m["rid"], m["kind"])
    msg["Reply-To"] = reply_to
    # RFC 2369 + RFC 8058 one-click: Gmail and Yahoo show "Unsubscribe" and POST List-Unsubscribe=One-Click.
    msg["List-Unsubscribe"] = f"<mailto:{reply_to}?subject=OPT%20OUT>, <{m['optout_link']}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg["X-IBLS-RID"] = m["rid"]
    msg["X-IBLS-Kind"] = m["kind"]
    if m["kind"] != "invite" and m.get("invite_msgid"):
        msg["In-Reply-To"] = m["invite_msgid"]
        msg["References"] = m["invite_msgid"]
    msg.set_content(text, charset="utf-8")
    msg.add_alternative(html, subtype="html", charset="utf-8")
    return msg


class MailboxSender:
    def __init__(self, mailbox: str, api: Api, run_id: str, *, dry_run=False, budget=540.0, jitter=(20.0, 60.0),
                 max_per_run=20, transport=None, transport_factory=None, smtp_factory=None, sleep=time.sleep,
                 clock=time.monotonic, now_fn=None):
        self.mailbox, self.api, self.run_id = mailbox, api, run_id
        self.dry_run, self.budget, self.jitter = dry_run, budget, jitter
        self.max_per_run = max_per_run
        if transport is not None:
            self._factory = lambda: transport
        elif smtp_factory is not None:  # SMTP fallback with an injected connection factory (tests)
            self._factory = lambda: SmtpTransport(mailbox, factory=smtp_factory)
        else:
            self._factory = transport_factory or (lambda: make_transport(mailbox))
        self.transport = None
        self.sleep, self.clock = sleep, clock
        # now_fn (local simulations only): a fake clock passed to the Worker as ?now= (honoured only with
        # ALLOW_TIME_OVERRIDE=1, never in production) and used for event timestamps.
        self.now_fn = now_fn
        self.stats = {"mailbox": mailbox, "sent": 0, "failed": 0, "hard": 0, "skipped": 0, "reason": "", "quota": False, "error": ""}

    def _ts(self) -> str:
        return iso(self.now_fn()) if self.now_fn else iso()

    def _close(self):
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                pass
        self.transport = None

    # --- main loop -----------------------------------------------------------
    def planned_max(self) -> int:
        avg = (self.jitter[0] + self.jitter[1]) / 2 or 1
        return max(0, min(self.max_per_run, int(self.budget // avg) + 1))

    def run(self) -> dict:
        start = self.clock()
        want = self.planned_max()
        params = {"mailbox": self.mailbox, "max": want, "run_id": self.run_id}
        if self.now_fn:
            params["now"] = iso(self.now_fn())
        batch = self.api.get("/api/next-batch", **params)
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
                if outcome in ("quota", "rate", "auth", "posting"):
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
        info: dict = {}
        for attempt in (1, 2):
            try:
                if self.transport is None:
                    self.transport = self._factory()
                info = self.transport.send(msg, m["email"]) or {}
                break
            except TransportError as e:
                cat, code, text = e.category, e.code, e.text
                if cat == "conn" and attempt == 1:
                    log.warning("[%s] connection problem (%s); reconnecting", self.mailbox, text)
                    self._close()
                    continue
                log.warning("[%s] %s -> %s: %s %s", self.mailbox, m["rid"], cat, code, text[:200])
                if cat == "rate":
                    # Short-term throttling: not the contact's fault. Nothing recorded; the reservation is released.
                    self.stats["reason"] = f"rate limited: {code} {text[:120]}"
                    return "rate"
                ev = {"rid": m["rid"], "email": m["email"], "mailbox": self.mailbox, "kind": m["kind"], "ts": self._ts(),
                      "meta": {**meta, "code": code, "error": text[:300]}}
                if cat == "hard":
                    ev.update(type="bounced_hard", kind="smtp", id=f"smtp-reject:{m['rid']}:{msg['Message-ID']}")
                    ev["meta"]["stage"] = "send"
                    self.stats["hard"] += 1
                else:
                    ev.update(type="failed")
                    self.stats["failed"] += 1
                self._post([ev])
                if cat == "quota":
                    self.stats["quota"] = True
                    self.stats["reason"] = f"sending quota/limit: {code} {text[:120]}"
                    return "quota"
                if cat == "auth":
                    self.stats["error"] = f"authentication/configuration failed for {self.mailbox}: {text[:160]}"
                    return "auth"
                return "hard" if cat == "hard" else "failed"
        else:
            return "failed"
        self.stats["sent"] += 1
        # Gmail may rewrite Message-ID; keep ours for de-duplication and record the one recipients see.
        if info.get("message_id") and info["message_id"] != msg["Message-ID"]:
            meta["our_message_id"] = msg["Message-ID"]
            meta["message_id"] = info["message_id"]
        for k in ("gmail_id", "thread_id"):
            if info.get(k):
                meta[k] = info[k]
        meta["transport"] = getattr(self.transport, "name", "unknown")
        ok = self._post([{"rid": m["rid"], "email": m["email"], "type": "sent", "kind": m["kind"], "mailbox": self.mailbox,
                          "ts": self._ts(), "meta": meta, "id": f"sent:{msg['Message-ID']}"}])
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
