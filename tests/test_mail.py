import unittest

from tests.helpers import FakeApi  # noqa: F401 (sets sys.path)
import mailclass  # noqa: E402
import sync_mail  # noqa: E402

DSN_HARD = b"""From: Mail Delivery System <MAILER-DAEMON@mx.mta.example.net>
To: participate@survo.tech
Subject: Undelivered Mail Returned to Sender
Message-ID: <dsn1@mx.mta.example.net>
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="BOUND"

--BOUND
Content-Type: text/plain

This is the mail system. Your message could not be delivered.

--BOUND
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.mta.example.net

Final-Recipient: rfc822; gone@acme.example
Original-Recipient: rfc822;gone@acme.example
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 <gone@acme.example>: Recipient address rejected: User unknown

--BOUND
Content-Type: text/rfc822-headers

From: India Business Leaders Survey <participate@survo.tech>
To: gone@acme.example
Subject: Research invitation: India Business Leaders Survey for Acme
X-IBLS-RID: IBGONE22
Message-ID: <ibls.IBGONE22.invite.abcdef123456@survo.tech>

--BOUND--
"""

DSN_SOFT = DSN_HARD.replace(b"Status: 5.1.1", b"Status: 4.2.2").replace(b"550 5.1.1", b"452 4.2.2").replace(b"User unknown", b"Mailbox full")
DSN_DELAYED = DSN_HARD.replace(b"Action: failed", b"Action: delayed").replace(b"Status: 5.1.1", b"Status: 4.4.1")

NONSTD_BOUNCE = b"""From: postmaster@company.example
To: research@survo.tech
Subject: Delivery has failed to these recipients or groups
Message-ID: <nd1@company.example>
Content-Type: text/plain

Your message to x@company.example couldn't be delivered. The email address wasn't found at the destination domain.
Remote server returned '550 5.1.10 RESOLVER.ADR.RecipientNotFound; Recipient not found by SMTP address lookup'

X-IBLS-RID: IBNOTF33
"""

ARF = b"""From: abuse@isp.example
To: participate@survo.tech
Subject: Abuse report
Message-ID: <arf1@isp.example>
MIME-Version: 1.0
Content-Type: multipart/report; report-type=feedback-report; boundary="B2"

--B2
Content-Type: text/plain

This is an email abuse report.

--B2
Content-Type: message/feedback-report

Feedback-Type: abuse
User-Agent: ISP-FBL/1.0
Version: 1
Original-Rcpt-To: <angry@isp.example>

--B2
Content-Type: message/rfc822

From: participate@survo.tech
To: angry@isp.example
X-IBLS-RID: IBSPAM44
Subject: Research invitation

body
--B2--
"""

OOO = b"""From: Ravi Kumar <ravi@acme.example>
To: participate@survo.tech
Subject: Automatic reply: Research invitation: India Business Leaders Survey for Acme
Auto-Submitted: auto-replied
In-Reply-To: <ibls.IBOOOO55.invite.abcdef123456@survo.tech>
Message-ID: <ooo1@acme.example>
Content-Type: text/plain

I am out of the office until Monday.
"""

OPTOUT = b"""From: Director <dir@acme.example>
To: participate@survo.tech
Subject: Re: Research invitation: India Business Leaders Survey for Acme
In-Reply-To: <ibls.IBOPTO66.invite.abcdef123456@survo.tech>
Message-ID: <opt1@acme.example>
Content-Type: text/plain

Please remove us from your list.

On Tue, 27 Oct 2026 at 10:05, India Business Leaders Survey <participate@survo.tech> wrote:
> To stop further emails, reply "OPT OUT"
"""

QUERY = b"""From: CFO <cfo@acme.example>
To: participate@survo.tech
Subject: Re: Research invitation: India Business Leaders Survey for Acme
References: <ibls.IBQUER77.invite.abcdef123456@survo.tech>
Message-ID: <q1@acme.example>
Content-Type: text/plain; charset=utf-8

Is the benchmark report shared with anyone else? Happy to participate if confidential.

On Tue, 27 Oct 2026 at 10:05, India Business Leaders Survey <participate@survo.tech> wrote:
> To stop further emails, reply "OPT OUT" or visit https://survo.tech/optout/IBQUER77.
"""

PLAIN_OPTOUT_SUBJECT = b"""From: someone@acme.example
To: participate@survo.tech
Subject: OPT OUT
Message-ID: <q2@acme.example>
Content-Type: text/plain

"""


class ClassifyTests(unittest.TestCase):
    def test_hard_dsn(self):
        c = mailclass.classify(DSN_HARD)
        self.assertEqual((c["type"], c["rid"], c["email"]), ("bounced_hard", "IBGONE22", "gone@acme.example"))
        self.assertEqual(c["detail"]["status"], "5.1.1")

    def test_soft_and_delayed(self):
        self.assertEqual(mailclass.classify(DSN_SOFT)["type"], "bounced_soft")
        self.assertEqual(mailclass.classify(DSN_DELAYED)["type"], "dsn_delayed")

    def test_nonstandard_bounce(self):
        c = mailclass.classify(NONSTD_BOUNCE)
        self.assertEqual((c["type"], c["rid"]), ("bounced_hard", "IBNOTF33"))

    def test_arf(self):
        c = mailclass.classify(ARF)
        self.assertEqual((c["type"], c["rid"], c["email"]), ("complaint", "IBSPAM44", "angry@isp.example"))

    def test_auto_reply(self):
        c = mailclass.classify(OOO)
        self.assertEqual((c["type"], c["rid"]), ("auto_reply", "IBOOOO55"))

    def test_optout_ignores_quoted_footer(self):
        c = mailclass.classify(OPTOUT)
        self.assertEqual((c["type"], c["rid"], c["email"]), ("opted_out", "IBOPTO66", "dir@acme.example"))
        q = mailclass.classify(QUERY)
        self.assertEqual((q["type"], q["rid"]), ("query", "IBQUER77"))  # quoted "OPT OUT" must not count
        self.assertIn("benchmark report", q["snippet"])
        self.assertEqual(mailclass.classify(PLAIN_OPTOUT_SUBJECT)["type"], "opted_out")

    def test_not_interested(self):
        raw = QUERY.replace(b"Is the benchmark report shared with anyone else? Happy to participate if confidential.", b"Not interested, thanks.")
        self.assertEqual(mailclass.classify(raw)["type"], "opted_out")

    def test_own_copy_ignored(self):
        raw = QUERY.replace(b"From: CFO <cfo@acme.example>", b"From: India Business Leaders Survey <participate@survo.tech>")
        self.assertEqual(mailclass.classify(raw)["type"], "ignore")


class FakeIMAP:
    """Minimal imaplib.IMAP4_SSL stand-in."""

    def __init__(self, messages, uidvalidity="777", capabilities=("IMAP4REV1", "MOVE")):
        self.messages = messages  # {uid: raw}
        self.uv = uidvalidity
        self.capabilities = capabilities
        self.searches, self.fetches, self.moved, self.readonly = [], [], [], None
        self.stored_flags = []

    def login(self, u, p):
        self.user = u

    def select(self, box, readonly=False):
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [self.uv.encode()]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.searches.append(args[-1])
            crit = args[-1]
            if crit.startswith("UID "):
                lo = int(crit.split()[1].split(":")[0])
                uids = [u for u in self.messages if u >= lo] or [max(self.messages)]  # IMAP returns last UID for n:* when none
            else:
                uids = list(self.messages)
            return "OK", [" ".join(str(u) for u in sorted(uids)).encode()]
        if cmd == "FETCH":
            u = int(args[0])
            self.fetches.append(args[1])
            return "OK", [(f'{u} (UID {u} INTERNALDATE "27-Oct-2026 10:15:02 +0530" BODY[] {{1}}'.encode(), self.messages[u]), b")"]
        if cmd == "MOVE":
            self.moved.append(args)
            return "OK", [b""]
        if cmd == "STORE":
            self.stored_flags.append(args)
            return "OK", [b""]
        raise AssertionError(cmd)

    def create(self, folder):
        return "OK", [b""]

    def logout(self):
        pass


class SyncMailTests(unittest.TestCase):
    def test_first_run_then_incremental(self):
        imap = FakeIMAP({101: DSN_HARD, 102: OOO, 103: OPTOUT, 104: QUERY, 105: ARF, 106: DSN_DELAYED})
        api = FakeApi()
        st = sync_mail.sync_mailbox(api, "participate@survo.tech", imap_factory=lambda: imap, file_noise=False)
        self.assertTrue(imap.readonly)
        self.assertTrue(imap.searches[0].startswith("SINCE"))
        self.assertTrue(all("BODY.PEEK" in f for f in imap.fetches))  # never sets \Seen
        self.assertEqual(imap.stored_flags, [])  # never flags/deletes
        types = [e["type"] for e in api.events_posted]
        self.assertEqual(types, ["bounced_hard", "auto_reply", "opted_out", "reply", "complaint"])  # delayed DSN not posted
        self.assertEqual(api.state_store["imap:participate@survo.tech"], "777:106")
        ev = api.events_posted[0]
        self.assertEqual(ev["id"], "mail:participate@survo.tech:777:101")
        self.assertEqual(ev["ts"], "2026-10-27T04:45:02.000Z")
        self.assertNotIn("from", ev["meta"])  # bounces stay out of the replies inbox
        self.assertEqual(api.events_posted[3]["meta"]["classification"], "query")
        self.assertEqual(st["fetched"], 6)

        # Second run: nothing new (server returns the last UID for "107:*").
        api.events_posted.clear()
        imap2 = FakeIMAP({101: DSN_HARD, 106: DSN_DELAYED})
        sync_mail.sync_mailbox(api, "participate@survo.tech", imap_factory=lambda: imap2, file_noise=False)
        self.assertEqual(imap2.searches[0], "UID 107:*")
        self.assertEqual(api.events_posted, [])

    def test_uidvalidity_change_restarts(self):
        api = FakeApi(state={"imap:participate@survo.tech": "111:50"})
        imap = FakeIMAP({3: QUERY}, uidvalidity="222")
        sync_mail.sync_mailbox(api, "participate@survo.tech", imap_factory=lambda: imap, file_noise=False)
        self.assertTrue(imap.searches[0].startswith("SINCE"))
        self.assertEqual(api.state_store["imap:participate@survo.tech"], "222:3")

    def test_file_noise_moves_but_keeps_queries(self):
        imap = FakeIMAP({1: DSN_HARD, 2: QUERY, 3: OOO})
        st = sync_mail.sync_mailbox(FakeApi(), "research@survo.tech", imap_factory=lambda: imap, file_noise=True)
        moved = {a[1]: a[0] for a in imap.moved}
        self.assertEqual(moved, {"IBLS/Auto-replies": "3", "IBLS/Bounces": "1"})
        self.assertEqual(st["moved"], 2)

    def test_cursor_not_advanced_when_posting_fails(self):
        api = FakeApi(fail_events=True)
        imap = FakeIMAP({5: QUERY})
        with self.assertRaises(RuntimeError):
            sync_mail.sync_mailbox(api, "participate@survo.tech", imap_factory=lambda: imap, file_noise=False)
        self.assertNotIn("imap:participate@survo.tech", api.state_store)


if __name__ == "__main__":
    unittest.main()
