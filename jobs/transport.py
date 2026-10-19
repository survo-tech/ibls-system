"""
Mail transports for the sender and the daily report.

  GmailApiTransport  default (MAIL_TRANSPORT=gmail_api): Gmail API users.messages.send as the mailbox, via a
                     service account with domain-wide delegation (GOOGLE_SA_JSON).
  SmtpTransport      fallback (MAIL_TRANSPORT=smtp): smtp.gmail.com:587 STARTTLS, logging in with the mailbox's
                     app password (GMAIL_APP_PW_<LOCALPART>).
  FakeTransport      tests and local end-to-end runs: records messages, sends nothing.

Every transport raises TransportError(category, code, text) on failure. Categories:
  hard   the recipient address was refused at send time (contact -> bounced)
  soft   this message failed; the contact may be retried
  quota  the mailbox hit its daily sending limit (mailbox stops for the rest of the IST day)
  rate   short-term throttling (stop this run, nothing recorded against the contact)
  auth   credentials / delegation / mailbox configuration problem (mailbox stops, job errors)
  conn   network problem (the sender reconnects and retries once)
Gmail keeps a copy of every message it sends in the mailbox's Sent folder, for both transports.
"""
from __future__ import annotations

import re
import smtplib
import socket
import ssl
import threading
from email.message import EmailMessage

from common import app_password, env, log, mail_transport
from gmail_api import SCOPES, GmailApiError, GmailClient, ServiceAccount, header_value

QUOTA_RE = re.compile(r"\b(5\.4\.\d+|4\.7\.\d+)\b|quota|limit exceeded|rate limit|too many (messages|recipients|connections)|sending limit", re.I)
DAILY_QUOTA_RE = re.compile(r"daily|sending limit|quota exceeded|dailyLimitExceeded|limit exceeded for sending", re.I)


class TransportError(Exception):
    def __init__(self, category: str, code: int | None, text: str):
        super().__init__(f"{category} {code} {text}")
        self.category, self.code, self.text = category, code, text


# --- SMTP ------------------------------------------------------------------------------------------
def _txt(v) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)


def classify_smtp_error(exc: Exception) -> tuple[str, int | None, str]:
    """-> (category, code, text) for smtplib / socket exceptions."""
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


def gmail_smtp_connect():
    s = smtplib.SMTP(env("SMTP_HOST", "smtp.gmail.com"), int(env("SMTP_PORT", "587")), timeout=60)
    s.ehlo()
    s.starttls(context=ssl.create_default_context())
    s.ehlo()
    return s


class SmtpTransport:
    name = "smtp"

    def __init__(self, mailbox: str, factory=None, password: str | None = None):
        self.mailbox = mailbox
        self.factory = factory or gmail_smtp_connect
        self.password = password
        self.conn = None

    def _connect(self):
        c = self.factory()
        try:
            c.login(self.mailbox, self.password or app_password(self.mailbox))
        except Exception:
            try:
                c.quit()
            except Exception:
                pass
            raise
        self.conn = c

    def send(self, msg: EmailMessage, rcpts) -> dict:
        rcpts = [rcpts] if isinstance(rcpts, str) else list(rcpts)
        try:
            if self.conn is None:
                self._connect()
            self.conn.send_message(msg, from_addr=self.mailbox, to_addrs=rcpts)
            return {}
        except Exception as e:  # noqa: BLE001 - classify everything SMTP can throw
            cat, code, text = classify_smtp_error(e)
            if cat in ("conn", "auth"):
                self.close()
            raise TransportError(cat, code, text) from None

    def close(self):
        if self.conn is not None:
            try:
                self.conn.quit()
            except Exception:
                pass
        self.conn = None


# --- Gmail API -------------------------------------------------------------------------------------
INVALID_RCPT_RE = re.compile(r"invalid (to|cc|bcc|recipient)|recipient address (is )?invalid|invalid email address", re.I)
AUTH_REASONS = {"unauthorized_client", "invalid_grant", "access_denied", "invalid_client", "insufficientPermissions",
                "forbidden", "failedPrecondition", "authError", "unauthorized"}
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "concurrentLimitExceeded", "backendError"}


def _retry_after_seconds(message: str, now: float | None = None) -> float:
    m = re.search(r"Retry after (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)", message)
    if not m:
        return 0.0
    import datetime as _dt
    import time as _time
    t = _dt.datetime.strptime(m.group(1).split(".")[0].rstrip("Z"), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc).timestamp()
    return t - (now if now is not None else _time.time())


def classify_gmail_error(exc: Exception) -> tuple[str, int | None, str]:
    """-> (category, code, text) for Gmail API / token errors and network exceptions."""
    if isinstance(exc, GmailApiError):
        st, reason, text = exc.status, exc.reason or "", f"{exc.reason}: {exc.message}"
        if exc.token_error:
            return ("conn" if st >= 500 else "auth"), st, text
        if DAILY_QUOTA_RE.search(exc.message or "") or reason in ("dailyLimitExceeded", "quotaExceeded"):
            return "quota", st, text
        if st == 429 or (st == 403 and reason in RATE_REASONS):
            # Gmail reports the per-user sending limit as "User-rate limit exceeded. Retry after <time>";
            # a retry time more than an hour away means the daily limit, not short-term throttling.
            return ("quota" if _retry_after_seconds(exc.message or "") > 3600 else "rate"), st, text
        if st in (401, 403) or reason in AUTH_REASONS or "Mail service not enabled" in (exc.message or ""):
            return "auth", st, text
        if st == 400 and INVALID_RCPT_RE.search(exc.message or ""):
            return "hard", st, text
        if st >= 500:
            return "conn", st, text
        return "soft", st, text
    if isinstance(exc, (socket.error, TimeoutError, ConnectionError)) or exc.__class__.__name__ == "URLError":
        return "conn", None, str(exc)
    return "soft", None, repr(exc)


class GmailApiTransport:
    name = "gmail_api"

    def __init__(self, mailbox: str, client: GmailClient | None = None, lookup_message_id: bool = True):
        self.mailbox = mailbox
        self._client = client
        self.lookup_message_id = lookup_message_id

    @property
    def client(self) -> GmailClient:
        if self._client is None:
            self._client = GmailClient(self.mailbox, ServiceAccount.shared(), SCOPES)
        return self._client

    def send(self, msg: EmailMessage, rcpts) -> dict:
        # Recipients come from the To/Cc headers of the raw message; Bcc is never used.
        try:
            r = self.client.send_raw(msg.as_bytes())
        except Exception as e:  # noqa: BLE001
            cat, code, text = classify_gmail_error(e)
            raise TransportError(cat, code, text) from None
        info = {"gmail_id": r.get("id"), "thread_id": r.get("threadId")}
        if self.lookup_message_id and r.get("id"):
            # Gmail may replace the Message-ID we set; record the one recipients actually see so reminders
            # thread correctly and replies can be matched. Best effort: never fail a sent message over it.
            try:
                meta = self.client.get_message(r["id"], fmt="metadata", headers=["Message-ID"])
                mid = header_value(meta.get("payload") or {}, "Message-ID")
                if mid:
                    info["message_id"] = mid.strip()
            except Exception as e:  # noqa: BLE001
                log.info("[%s] could not read back Message-ID: %s", self.mailbox, e)
        return info

    def close(self):
        pass


# --- Fake ------------------------------------------------------------------------------------------
class FakeTransport:
    """Records messages instead of sending. `script` maps recipient -> TransportError (or a callable returning one)."""
    name = "fake"
    lock = threading.Lock()

    def __init__(self, mailbox: str = "", script: dict | None = None, sink: list | None = None):
        self.mailbox = mailbox
        self.script = script or {}
        self.sent: list[EmailMessage] = []
        self.sink = sink
        self.closed = False

    def send(self, msg: EmailMessage, rcpts) -> dict:
        rcpt = rcpts if isinstance(rcpts, str) else rcpts[0]
        err = self.script.get(rcpt)
        if callable(err) and not isinstance(err, BaseException):
            err = err()
        if err is not None:
            raise err
        self.sent.append(msg)
        if self.sink is not None:
            with self.lock:
                self.sink.append((self.mailbox, msg))
        return {"gmail_id": f"fake-{len(self.sent)}", "thread_id": f"fake-t-{len(self.sent)}"}

    def close(self):
        self.closed = True


def make_transport(mailbox: str, kind: str | None = None):
    """Transport for a mailbox according to MAIL_TRANSPORT."""
    kind = kind or mail_transport()
    if kind == "smtp":
        return SmtpTransport(mailbox)
    if kind == "fake":
        base = env("API_BASE", "")
        if not re.match(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", base):
            raise SystemExit("MAIL_TRANSPORT=fake is only allowed against a local Worker (API_BASE=http://127.0.0.1:…)")
        return FakeTransport(mailbox)
    return GmailApiTransport(mailbox)
