"""Gmail transport (mocked HTTP), mailbox rotation, Gmail DSN classification and Gmail API sync."""
import base64
import datetime as dt
import email
import json
import os
import unittest
import urllib.parse
from email import policy
from unittest import mock

from tests.helpers import FakeApi, FakeSMTP  # noqa: F401 (sets sys.path)
import daily_report  # noqa: E402
import gmail_api  # noqa: E402
import mailclass  # noqa: E402
import send  # noqa: E402
import sync_mail  # noqa: E402
import transport  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402
from tests.test_send import SETTINGS, msg  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
SA_INFO = {"type": "service_account", "client_email": "ibls-mailer@ibls-mailer.iam.gserviceaccount.com", "private_key": PEM,
           "private_key_id": "kid123", "token_uri": "https://oauth2.googleapis.com/token"}
FIVE = ["participate@survo.tech", "research@survo.tech", "survey@survo.tech", "study@survo.tech", "team@survo.tech"]


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakeHttp:
    """Routes (method, path-without-query) to handlers; records every call."""

    def __init__(self):
        self.calls = []
        self.routes = {}
        self.tokens = 0

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        if url == SA_INFO["token_uri"]:
            self.tokens += 1
            h = self.routes.get(("POST", "token"))
            if h:
                return h(body)
            return 200, json.dumps({"access_token": f"tok-{self.tokens}", "expires_in": 3599, "token_type": "Bearer"}).encode()
        path = urllib.parse.urlsplit(url).path.replace("/gmail/v1/users/me", "")
        for (m, p), h in self.routes.items():
            if m == method and (p == path or (p.endswith("*") and path.startswith(p[:-1]))):
                return h(url, headers, body)
        return 404, json.dumps({"error": {"code": 404, "message": "Not Found", "errors": [{"reason": "notFound"}]}}).encode()


def gerr(code, reason, message):
    return code, json.dumps({"error": {"code": code, "message": message, "errors": [{"reason": reason, "message": message}]}}).encode()


def client_for(mailbox, http):
    sa = gmail_api.ServiceAccount(SA_INFO, http=http)
    return gmail_api.GmailClient(mailbox, sa, gmail_api.SCOPES, http=http)


class ServiceAccountTests(unittest.TestCase):
    def test_jwt_assertion_and_token_cache(self):
        http = FakeHttp()
        sa = gmail_api.ServiceAccount(SA_INFO, http=http, clock=lambda: 1_800_000_000)
        tok = sa.token("study@survo.tech")
        self.assertEqual(tok, "tok-1")
        self.assertEqual(sa.token("study@survo.tech"), "tok-1")  # cached
        self.assertEqual(http.tokens, 1)
        sa.token("team@survo.tech")  # different subject -> new token
        self.assertEqual(http.tokens, 2)
        form = urllib.parse.parse_qs(http.calls[0][3].decode())
        self.assertEqual(form["grant_type"], ["urn:ietf:params:oauth:grant-type:jwt-bearer"])
        head, claims, sig = form["assertion"][0].split(".")
        self.assertEqual(json.loads(b64d(head)), {"alg": "RS256", "typ": "JWT", "kid": "kid123"})
        c = json.loads(b64d(claims))
        self.assertEqual(c["iss"], SA_INFO["client_email"])
        self.assertEqual(c["sub"], "study@survo.tech")  # domain-wide delegation: impersonate the mailbox
        self.assertEqual(c["aud"], "https://oauth2.googleapis.com/token")
        self.assertEqual(c["scope"].split(), ["https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly",
                                              "https://www.googleapis.com/auth/gmail.modify"])
        self.assertEqual(c["exp"] - c["iat"], 3600)
        # Signature verifies with the service account's public key.
        KEY.public_key().verify(b64d(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())

    def test_from_env_requires_secret(self):
        with mock.patch.dict(os.environ, {"GOOGLE_SA_JSON": "", "GOOGLE_SA_FILE": ""}):
            with self.assertRaises(SystemExit):
                gmail_api.ServiceAccount.from_env()
        with mock.patch.dict(os.environ, {"GOOGLE_SA_JSON": base64.b64encode(json.dumps(SA_INFO).encode()).decode()}):
            self.assertEqual(gmail_api.ServiceAccount.from_env().email, SA_INFO["client_email"])

    def test_token_error_is_auth(self):
        http = FakeHttp()
        http.routes[("POST", "token")] = lambda body: (401, json.dumps({"error": "unauthorized_client",
                                                                        "error_description": "Client is unauthorized to retrieve access tokens"}).encode())
        t = transport.GmailApiTransport("team@survo.tech", client=client_for("team@survo.tech", http))
        with self.assertRaises(transport.TransportError) as cm:
            t.send(send.build_message(msg(), SETTINGS, "team@survo.tech"), "ceo@acme.example")
        self.assertEqual(cm.exception.category, "auth")


class GmailTransportTests(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        self.sent_raw = []

        def send_h(url, headers, body):
            self.assertEqual(headers["Authorization"], "Bearer tok-1")
            self.sent_raw.append(b64d(json.loads(body)["raw"]))
            return 200, json.dumps({"id": "18f0a1", "threadId": "18f0a1", "labelIds": ["SENT"]}).encode()

        def get_h(url, headers, body):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertEqual(q["format"], ["metadata"])
            self.assertEqual(q["metadataHeaders"], ["Message-ID"])
            return 200, json.dumps({"id": "18f0a1", "payload": {"headers": [{"name": "Message-Id", "value": "<CAGmail123@mail.gmail.com>"}]}}).encode()
        self.http.routes[("POST", "/messages/send")] = send_h
        self.http.routes[("GET", "/messages/*")] = get_h

    def test_send_raw_message_and_record_gmail_ids(self):
        api = FakeApi(batches={"survey@survo.tech": {"messages": [msg(mailbox="survey@survo.tech")], "settings": SETTINGS, "run_id": "r:survey@survo.tech"}})
        t = transport.GmailApiTransport("survey@survo.tech", client=client_for("survey@survo.tech", self.http))
        st = send.MailboxSender("survey@survo.tech", api, "r", jitter=(0, 0), transport=t, sleep=lambda s: None).run()
        self.assertEqual(st["sent"], 1)
        m = email.message_from_bytes(self.sent_raw[0], policy=policy.default)
        self.assertEqual(m["From"], "India Business Leaders Survey <survey@survo.tech>")
        self.assertEqual(m["To"], "ceo@acme.example")
        self.assertEqual(m["Reply-To"], "research@survo.tech")
        self.assertEqual(m["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertIn("https://survo.tech/optout/IBAAAA22", m["List-Unsubscribe"])
        self.assertIsNone(m["Bcc"])
        ev = api.events_posted[0]
        self.assertEqual(ev["type"], "sent")
        self.assertEqual(ev["mailbox"], "survey@survo.tech")
        self.assertEqual(ev["meta"]["transport"], "gmail_api")
        self.assertEqual(ev["meta"]["gmail_id"], "18f0a1")
        # Gmail rewrote the Message-ID: the event carries the one recipients see; dedupe still uses ours.
        self.assertEqual(ev["meta"]["message_id"], "<CAGmail123@mail.gmail.com>")
        self.assertRegex(ev["meta"]["our_message_id"], r"^<ibls\.IBAAAA22\.invite\.")
        self.assertEqual(ev["id"], "sent:" + ev["meta"]["our_message_id"])

    def test_retries_token_once_on_401(self):
        n = {"send": 0}
        orig = self.http.routes[("POST", "/messages/send")]

        def flaky(url, headers, body):
            n["send"] += 1
            if n["send"] == 1:
                return gerr(401, "authError", "Invalid Credentials")
            self.assertEqual(headers["Authorization"], "Bearer tok-2")
            return 200, json.dumps({"id": "x1", "threadId": "x1"}).encode()
        self.http.routes[("POST", "/messages/send")] = flaky
        t = transport.GmailApiTransport("team@survo.tech", client=client_for("team@survo.tech", self.http), lookup_message_id=False)
        info = t.send(send.build_message(msg(), SETTINGS, "team@survo.tech"), "ceo@acme.example")
        self.assertEqual(info["gmail_id"], "x1")
        self.assertEqual(self.http.tokens, 2)
        self.http.routes[("POST", "/messages/send")] = orig

    def test_error_classification(self):
        soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        E = gmail_api.GmailApiError
        cases = [
            (E(429, "rateLimitExceeded", f"User-rate limit exceeded.  Retry after {soon}"), "rate"),
            (E(429, "rateLimitExceeded", f"User-rate limit exceeded.  Retry after {later}"), "quota"),
            (E(403, "dailyLimitExceeded", "Daily Limit Exceeded"), "quota"),
            (E(400, "failedPrecondition", "Mail sending limit exceeded for sending"), "quota"),
            (E(400, "invalidArgument", "Invalid To header"), "hard"),
            (E(400, "failedPrecondition", "Precondition check failed."), "auth"),
            (E(403, "insufficientPermissions", "Request had insufficient authentication scopes."), "auth"),
            (E(401, "unauthorized_client", "Client is unauthorized", token_error=True), "auth"),
            (E(503, "backendError", "Backend Error"), "conn"),
            (E(400, "invalidArgument", "Invalid value for ByteString"), "soft"),
            (ConnectionResetError("reset"), "conn"),
        ]
        for exc, want in cases:
            self.assertEqual(transport.classify_gmail_error(exc)[0], want, exc)

    def test_sender_rate_limit_records_nothing_and_releases(self):
        self.http.routes[("POST", "/messages/send")] = lambda u, h, b: gerr(429, "rateLimitExceeded", "User-rate limit exceeded.")
        api = FakeApi(batches={"team@survo.tech": {"messages": [msg(), msg("IBBBBB33", "b@x.example")], "settings": SETTINGS, "run_id": "r:team@survo.tech"}})
        t = transport.GmailApiTransport("team@survo.tech", client=client_for("team@survo.tech", self.http))
        st = send.MailboxSender("team@survo.tech", api, "r", jitter=(0, 0), transport=t, sleep=lambda s: None).run()
        self.assertEqual((st["sent"], st["failed"], st["quota"]), (0, 0, False))
        self.assertIn("rate limited", st["reason"])
        self.assertEqual(api.events_posted, [])
        self.assertIn(("POST", "/api/release", {"run_id": "r:team@survo.tech"}), api.calls)

    def test_sender_daily_quota_marks_mailbox(self):
        self.http.routes[("POST", "/messages/send")] = lambda u, h, b: gerr(403, "dailyLimitExceeded", "Daily Limit Exceeded")
        api = FakeApi(batches={"team@survo.tech": {"messages": [msg()], "settings": SETTINGS}})
        t = transport.GmailApiTransport("team@survo.tech", client=client_for("team@survo.tech", self.http))
        st = send.MailboxSender("team@survo.tech", api, "r", jitter=(0, 0), transport=t, sleep=lambda s: None).run()
        self.assertTrue(st["quota"])
        self.assertEqual([e["type"] for e in api.events_posted], ["failed"])

    def test_invalid_recipient_is_hard_bounce(self):
        self.http.routes[("POST", "/messages/send")] = lambda u, h, b: gerr(400, "invalidArgument", "Invalid To header")
        api = FakeApi(batches={"team@survo.tech": {"messages": [msg()], "settings": SETTINGS}})
        t = transport.GmailApiTransport("team@survo.tech", client=client_for("team@survo.tech", self.http))
        st = send.MailboxSender("team@survo.tech", api, "r", jitter=(0, 0), transport=t, sleep=lambda s: None).run()
        self.assertEqual(st["hard"], 1)
        self.assertEqual((api.events_posted[0]["type"], api.events_posted[0]["kind"]), ("bounced_hard", "smtp"))


class TransportSelectionTests(unittest.TestCase):
    def test_make_transport_modes(self):
        with mock.patch.dict(os.environ, {"MAIL_TRANSPORT": "smtp"}):
            self.assertIsInstance(transport.make_transport("study@survo.tech"), transport.SmtpTransport)
        with mock.patch.dict(os.environ, {"MAIL_TRANSPORT": ""}):
            self.assertIsInstance(transport.make_transport("study@survo.tech"), transport.GmailApiTransport)
        with mock.patch.dict(os.environ, {"MAIL_TRANSPORT": "carrier-pigeon"}):
            with self.assertRaises(SystemExit):
                transport.make_transport("study@survo.tech")

    def test_fake_transport_only_against_local_worker(self):
        with mock.patch.dict(os.environ, {"API_BASE": "https://survo.tech"}):
            with self.assertRaises(SystemExit):
                transport.make_transport("study@survo.tech", kind="fake")
        with mock.patch.dict(os.environ, {"API_BASE": "http://127.0.0.1:8788"}):
            self.assertIsInstance(transport.make_transport("study@survo.tech", kind="fake"), transport.FakeTransport)

    def test_smtp_fallback_uses_gmail_starttls_and_app_password(self):
        made = []

        class FakeConn(FakeSMTP):
            def __init__(self, host, port, timeout=None):
                super().__init__()
                self.host, self.port, self.tls = host, port, False

            def ehlo(self):
                pass

            def starttls(self, context=None):
                self.tls = True
        with mock.patch.object(transport.smtplib, "SMTP", side_effect=lambda h, p, timeout=None: made.append(FakeConn(h, p)) or made[-1]):
            t = transport.SmtpTransport("study@survo.tech")
            t.send(send.build_message(msg(), SETTINGS, "study@survo.tech"), "ceo@acme.example")
            t.close()
        c = made[0]
        self.assertEqual((c.host, c.port, c.tls), ("smtp.gmail.com", 587, True))
        self.assertEqual(c.logged_in, ("study@survo.tech", "pw-study"))  # GMAIL_APP_PW_STUDY
        self.assertEqual(len(c.sent), 1)
        self.assertTrue(c.closed)


class RotationTests(unittest.TestCase):
    def test_all_five_mailboxes_send_in_parallel_with_their_own_address(self):
        batches = {mb: {"messages": [msg(f"IBROT{i}22", f"r{i}@x.example", mailbox=mb)], "settings": SETTINGS, "run_id": f"x:{mb}"}
                   for i, mb in enumerate(FIVE)}
        api = FakeApi(batches=batches)
        sink = []
        script = {"r4@x.example": transport.TransportError("quota", 550, "5.4.5 Daily user sending quota exceeded")}
        with mock.patch.object(send, "Api", return_value=api), mock.patch.object(send, "MAILBOXES", FIVE), \
                mock.patch.object(send, "make_transport", side_effect=lambda mb: transport.FakeTransport(mb, script=script, sink=sink)), \
                mock.patch.dict(os.environ, {"JITTER_MIN": "0", "JITTER_MAX": "0"}):
            rc = send.main()
        self.assertEqual(rc, 0)  # a quota on one mailbox is not a job error
        self.assertEqual(sorted(mb for mb, _ in sink), sorted(FIVE[:4]))
        for mb, m in sink:
            self.assertEqual(email.utils.parseaddr(m["From"])[1], mb)
        asked = sorted(c[2]["mailbox"] for c in api.calls if c[1] == "/api/next-batch")
        self.assertEqual(asked, sorted(FIVE))
        quota_runs = [jr for jr in api.job_runs if jr[3].get("quota_mailbox")]
        self.assertEqual([jr[3]["quota_mailbox"] for jr in quota_runs], ["team@survo.tech"])

    def test_default_mailboxes_are_the_five_workspace_users(self):
        import common
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAILBOXES", None)
            self.assertEqual([m.strip() for m in common.DEFAULT_MAILBOXES.split(",")], FIVE)
        self.assertEqual(common.app_password_env("study@survo.tech"), "GMAIL_APP_PW_STUDY")


# --- DSN classification (Gmail's own bounce format) ---------------------------------------------------
GMAIL_DSN = b"""Delivered-To: survey@survo.tech
From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>
To: survey@survo.tech
X-Failed-Recipients: nobody@acme.example
Subject: Delivery Status Notification (Failure)
Message-ID: <6513a2f0.abc@mx.google.com>
Auto-Submitted: auto-replied
MIME-Version: 1.0
Content-Type: multipart/report; boundary="000000000000dsn"; report-type=delivery-status

--000000000000dsn
Content-Type: multipart/related; boundary="000000000000rel"

--000000000000rel
Content-Type: text/html; charset="UTF-8"

<html><body><h2>Address not found</h2><p>Your message wasn't delivered to nobody@acme.example because the address couldn't be found.</p></body></html>
--000000000000rel--

--000000000000dsn
Content-Type: message/delivery-status

Reporting-MTA: dns; googlemail.com
Received-From-MTA: dns; survey@survo.tech
Arrival-Date: Tue, 27 Oct 2026 02:10:01 -0700 (PDT)
X-Original-Message-ID: <ibls.IBDSNG55.invite.0a1b2c3d4e5f@survo.tech>

Final-Recipient: rfc822; nobody@acme.example
Action: failed
Status: 5.1.1
Remote-MTA: dns; mx.acme.example. (203.0.113.5, the server for the domain acme.example.)
Diagnostic-Code: smtp; 550 5.1.1 The email account that you tried to reach does not exist.
Last-Attempt-Date: Tue, 27 Oct 2026 02:10:02 -0700 (PDT)

--000000000000dsn
Content-Type: message/rfc822

From: India Business Leaders Survey <survey@survo.tech>
To: nobody@acme.example
Subject: Research invitation: India Business Leaders Survey for Acme
Message-ID: <ibls.IBDSNG55.invite.0a1b2c3d4e5f@survo.tech>
X-IBLS-RID: IBDSNG55
X-IBLS-Kind: invite

Dear Director,
--000000000000dsn--
"""

GMAIL_DELAY = GMAIL_DSN.replace(b"(Failure)", b"(Delay)").replace(b"Action: failed", b"Action: delayed").replace(b"Status: 5.1.1", b"Status: 4.4.7")
GMAIL_FULL = GMAIL_DSN.replace(b"Status: 5.1.1", b"Status: 5.2.2").replace(b"550 5.1.1 The email account that you tried to reach does not exist.",
                                                                            b"552 5.2.2 The recipient's inbox is out of storage space.")
GMAIL_NO_FINAL = GMAIL_DSN.replace(b"Final-Recipient: rfc822; nobody@acme.example\n", b"")
GMAIL_DNS = GMAIL_DSN.replace(b"Status: 5.1.1", b"Status: 5.1.2").replace(
    b"550 5.1.1 The email account that you tried to reach does not exist.", b"DNS Error: Domain name not found")


class GmailDsnTests(unittest.TestCase):
    def test_gmail_hard_bounce(self):
        c = mailclass.classify(GMAIL_DSN)
        self.assertEqual((c["type"], c["rid"], c["email"]), ("bounced_hard", "IBDSNG55", "nobody@acme.example"))
        self.assertEqual(c["detail"]["status"], "5.1.1")

    def test_gmail_delay_is_not_a_bounce(self):
        self.assertEqual(mailclass.classify(GMAIL_DELAY)["type"], "dsn_delayed")

    def test_gmail_mailbox_full_is_soft(self):
        self.assertEqual(mailclass.classify(GMAIL_FULL)["type"], "bounced_soft")

    def test_x_failed_recipients_fallback(self):
        c = mailclass.classify(GMAIL_NO_FINAL)
        self.assertEqual((c["type"], c["email"]), ("bounced_hard", "nobody@acme.example"))

    def test_domain_not_found_is_hard(self):
        self.assertEqual(mailclass.classify(GMAIL_DNS)["type"], "bounced_hard")

    def test_dsn_event_stays_out_of_replies(self):
        c = mailclass.classify(GMAIL_DSN)
        ev = sync_mail.to_event(c, "survey@survo.tech", "mail:x", "2026-10-27T09:10:02.000Z")
        self.assertEqual(ev["type"], "bounced_hard")
        self.assertNotIn("from", ev["meta"])
        self.assertNotIn("subject", ev["meta"])

    def test_mail_from_any_own_mailbox_is_ignored(self):
        raw = b"From: India Business Leaders Survey <team@survo.tech>\nTo: research@survo.tech\nSubject: hello\n\nhi\n"
        self.assertEqual(mailclass.classify(raw)["type"], "ignore")


# --- Gmail API sync ----------------------------------------------------------------------------------
REPLY_NO_HEADERS = b"""From: CFO <cfo@acme.example>
To: research@survo.tech
Subject: Re: Research invitation
Message-ID: <reply1@acme.example>
In-Reply-To: <CAGmail123@mail.gmail.com>
Content-Type: text/plain

Who else will see our answers? Happy to take part if it is confidential.
"""
OOO = b"""From: Director <dir@acme.example>
To: survey@survo.tech
Subject: Automatic reply: Research invitation
Auto-Submitted: auto-replied
Message-ID: <ooo1@acme.example>
X-IBLS-RID: IBOOOO11
Content-Type: text/plain

I am out of the office until Monday.
"""
OPTOUT = b"""From: MD <md@acme.example>
To: research@survo.tech
Subject: Re: Research invitation
Message-ID: <oo2@acme.example>
In-Reply-To: <ibls.IBOPTO66.invite.aaaaaaaaaaaa@survo.tech>
Content-Type: text/plain

Please remove me from your list.
"""
OWN_SENT = b"From: India Business Leaders Survey <survey@survo.tech>\nTo: x@acme.example\nSubject: Research invitation\n\nhi\n"


class FakeGmailClient:
    def __init__(self, mailbox, messages, history_id="5000", history=None, threads=None, expired=False):
        self.mailbox = mailbox
        self.messages = messages  # id -> (raw, labels, internalDate ms, threadId)
        self.history_id = history_id
        self.history = history or []
        self.threads = threads or {}
        self.expired = expired
        self.queries, self.history_calls, self.modified, self.created = [], [], [], []
        self.labels = [{"id": "INBOX", "name": "INBOX"}, {"id": "Label_1", "name": "IBLS/Bounces"}]

    def profile(self):
        return {"emailAddress": self.mailbox, "historyId": self.history_id}

    def list_messages(self, q, page_token=None, max_results=100):
        self.queries.append(q)
        ids = sorted(self.messages, key=lambda i: self.messages[i][2], reverse=True)
        return {"messages": [{"id": i, "threadId": self.messages[i][3]} for i in ids]}

    def list_history(self, start, page_token=None):
        self.history_calls.append(start)
        if self.expired:
            raise gmail_api.GmailApiError(404, "notFound", "Requested entity was not found.")
        return {"history": self.history, "historyId": self.history_id}

    def get_message(self, mid, fmt="raw", headers=None):
        raw, labels, ts, thread = self.messages[mid]
        return {"id": mid, "threadId": thread, "labelIds": labels, "internalDate": str(ts), "raw": gmail_api.b64url(raw)}

    def get_thread(self, tid, headers):
        return {"id": tid, "messages": [{"payload": {"headers": [{"name": "X-IBLS-RID", "value": r}]}} for r in self.threads.get(tid, [])]}

    def list_labels(self):
        return self.labels

    def create_label(self, name):
        lab = {"id": f"Label_{len(self.labels) + 1}", "name": name}
        self.labels.append(lab)
        self.created.append(name)
        return lab

    def modify(self, mid, add=None, remove=None):
        self.modified.append((mid, tuple(add or []), tuple(remove or [])))
        return {}


T0 = 1_793_000_000_000  # ms


class GmailSyncTests(unittest.TestCase):
    def msgs(self):
        return {
            "m1": (GMAIL_DSN, ["INBOX", "UNREAD"], T0 + 1000, "t1"),
            "m2": (OOO, ["INBOX", "UNREAD"], T0 + 2000, "t2"),
            "m3": (REPLY_NO_HEADERS, ["INBOX", "UNREAD"], T0 + 3000, "t3"),
            "m4": (OPTOUT, ["INBOX"], T0 + 4000, "t4"),
            "m5": (OWN_SENT, ["SENT"], T0 + 5000, "t5"),
        }

    def test_first_run_queries_since_start_date_then_uses_history(self):
        api = FakeApi()
        c = FakeGmailClient("research@survo.tech", self.msgs(), history_id="5000", threads={"t3": ["IBREPL44"]})
        st = sync_mail.sync_mailbox_gmail(api, "research@survo.tech", client=c, file_noise=False)
        start = int(dt.datetime(2026, 10, 20, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))).timestamp())
        self.assertEqual(c.queries, [f"in:inbox after:{start}"])
        types = [(e["type"], e["rid"]) for e in api.events_posted]
        self.assertEqual(types, [("bounced_hard", "IBDSNG55"), ("auto_reply", "IBOOOO11"), ("reply", "IBREPL44"), ("opted_out", "IBOPTO66")])
        self.assertEqual(st["fetched"], 4)  # own sent copy skipped
        self.assertEqual(api.events_posted[0]["id"], "mail:research@survo.tech:gmail:m1")
        self.assertEqual(api.events_posted[0]["ts"], "2026-10-26T07:33:21.000Z")
        self.assertEqual(api.events_posted[2]["meta"]["classification"], "query")
        saved = json.loads(api.state_store["gmail:research@survo.tech"])
        self.assertEqual(saved, {"history_id": "5000", "synced_at": (T0 + 5000) // 1000})
        self.assertEqual(c.modified, [])  # never touches labels unless FILE_NOISE

        # Second run: history since 5000 brings one new message.
        api.events_posted.clear()
        msgs = self.msgs()
        msgs["m6"] = (OOO.replace(b"<ooo1@", b"<ooo2@"), ["INBOX"], T0 + 9000, "t6")
        c2 = FakeGmailClient("research@survo.tech", msgs, history_id="5100",
                             history=[{"id": "5050", "messagesAdded": [{"message": {"id": "m6", "labelIds": ["INBOX", "UNREAD"]}}]}])
        st2 = sync_mail.sync_mailbox_gmail(api, "research@survo.tech", client=c2, file_noise=False)
        self.assertEqual(c2.history_calls, ["5000"])
        self.assertEqual(c2.queries, [])
        self.assertEqual(st2["mode"], "history")
        self.assertEqual([e["id"] for e in api.events_posted], ["mail:research@survo.tech:gmail:m6"])
        self.assertEqual(json.loads(api.state_store["gmail:research@survo.tech"])["history_id"], "5100")

    def test_expired_history_falls_back_to_query(self):
        api = FakeApi(state={"gmail:survey@survo.tech": json.dumps({"history_id": "10", "synced_at": 1_793_000_000})})
        c = FakeGmailClient("survey@survo.tech", {"m2": self.msgs()["m2"]}, history_id="9000", expired=True)
        st = sync_mail.sync_mailbox_gmail(api, "survey@survo.tech", client=c, file_noise=False)
        self.assertEqual(c.queries, [f"in:inbox after:{1_793_000_000 - 3600}"])
        self.assertEqual(st["mode"], "query (history expired)")
        self.assertEqual(json.loads(api.state_store["gmail:survey@survo.tech"])["history_id"], "9000")

    def test_cursor_not_advanced_when_posting_fails(self):
        api = FakeApi(fail_events=True)
        c = FakeGmailClient("research@survo.tech", self.msgs())
        with self.assertRaises(RuntimeError):
            sync_mail.sync_mailbox_gmail(api, "research@survo.tech", client=c, file_noise=False)
        self.assertNotIn("gmail:research@survo.tech", api.state_store)

    def test_file_noise_labels_and_archives_but_keeps_queries_unread(self):
        api = FakeApi()
        c = FakeGmailClient("research@survo.tech", self.msgs())
        st = sync_mail.sync_mailbox_gmail(api, "research@survo.tech", client=c, file_noise=True)
        self.assertEqual(c.created, ["IBLS/Auto-replies"])  # IBLS/Bounces already existed
        moved = {m: (add, rem) for m, add, rem in c.modified}
        self.assertEqual(set(moved), {"m1", "m2"})  # bounce + auto-reply; the query and the opt-out stay in INBOX
        self.assertEqual(moved["m1"], (("Label_1",), ("INBOX",)))
        self.assertTrue(all("UNREAD" not in rem for _, rem in moved.values()))
        self.assertEqual(st["moved"], 2)

    def test_history_truncation_stops_at_record_boundary(self):
        msgs = {f"h{i}": (OOO.replace(b"<ooo1@", f"<o{i}@".encode()), ["INBOX"], T0 + i, f"t{i}") for i in range(3)}
        hist = [{"id": str(6000 + i), "messagesAdded": [{"message": {"id": f"h{i}", "labelIds": ["INBOX"]}}]} for i in range(3)]
        api = FakeApi(state={"gmail:team@survo.tech": json.dumps({"history_id": "5999", "synced_at": 1})})
        c = FakeGmailClient("team@survo.tech", msgs, history_id="7000", history=hist)
        st = sync_mail.sync_mailbox_gmail(api, "team@survo.tech", client=c, max_msgs=2, file_noise=False)
        self.assertTrue(st["truncated"])
        self.assertEqual(len(api.events_posted), 2)
        self.assertEqual(json.loads(api.state_store["gmail:team@survo.tech"])["history_id"], "6001")


class DailyReportTests(unittest.TestCase):
    def test_report_goes_through_the_transport(self):
        api = FakeApi()
        api.summary = {"day": "2026-10-27", "paused": False, "test_mode": False,
                       "today": {"invites": 100, "reminders": 0, "failed": 0, "hard": 1, "soft": 0, "clicks": 3, "completed": 1, "declined": 0, "opted_out": 0, "complaints": 0},
                       "totals": {"invited": 100, "sendable": 7969}, "jobs": [],
                       "mailboxes": [{"mailbox": mb, "sent_today": 20, "daily_cap": 20, "stage_label": "stage 1 (20/day)"} for mb in FIVE]}
        t = transport.FakeTransport("research@survo.tech")
        with mock.patch.dict(os.environ, {"REPORT_TO": "a@london.edu, b@mit.edu"}):
            rc = daily_report.main(api=api, transport=t)
        self.assertEqual(rc, 0)
        m = t.sent[0]
        self.assertEqual(m["To"], "a@london.edu, b@mit.edu")
        self.assertIn("research@survo.tech", m["From"])
        body = m.get_content()
        self.assertIn("team@survo.tech", body)
        self.assertIn("stage 1 (20/day)", body)
        self.assertTrue(t.closed)


if __name__ == "__main__":
    unittest.main()


class SafetyNetTests(unittest.TestCase):
    def test_tests_cannot_reach_google(self):
        sa = gmail_api.ServiceAccount(SA_INFO)  # no http injected -> the module default, disabled in tests
        with self.assertRaises(AssertionError):
            sa.token("participate@survo.tech")

    def test_sync_refuses_fake_transport(self):
        with mock.patch.dict(os.environ, {"MAIL_TRANSPORT": "fake", "API_BASE": "http://127.0.0.1:8787"}), \
                mock.patch.object(sync_mail, "Api", return_value=FakeApi()):
            with self.assertRaises(SystemExit):
                sync_mail.main()
