"""
Integration test: the real Python jobs against a running local Worker (wrangler dev --local),
with SMTP/IMAP/Kobo faked. Skipped unless IBLS_BASE is set (scripts/local_e2e.sh sets it).
Expects a FRESH local DB with the two seeded test contacts (TEST0001 participate@, TEST0002 research@).
"""
import base64
import json
import os
import unittest
import urllib.request

from tests.helpers import FakeSMTP
from tests.test_mail import DSN_HARD, FakeIMAP
from tests.test_kobo_report_import import FakeResp

BASE = os.environ.get("IBLS_BASE")


def dash(path):
    req = urllib.request.Request(BASE + "/dashboard/api/" + path, headers={"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


@unittest.skipUnless(BASE, "set IBLS_BASE to run against wrangler dev")
class JobsAgainstLocalWorker(unittest.TestCase):
    def test_send_sync_kobo_flow(self):
        import send
        import sync_kobo
        import sync_mail
        from common import Api

        api = Api(base=BASE, token=os.environ.get("IBLS_TOKEN", "local-test-token-0123456789"))
        smtps = {}
        results = {}
        for mb in ("participate@survo.tech", "research@survo.tech"):
            smtp = FakeSMTP()
            smtps[mb] = smtp
            s = send.MailboxSender(mb, api, "it-run", jitter=(0, 0), smtp_factory=lambda smtp=smtp: smtp, sleep=lambda x: None)
            results[mb] = s.run()
        # test mode: exactly the two seeded test contacts, one per mailbox
        self.assertEqual(results["participate@survo.tech"]["sent"], 1, results)
        self.assertEqual(results["research@survo.tech"]["sent"], 1, results)
        m = smtps["participate@survo.tech"].sent[0]
        self.assertEqual(m["To"], "tester1@example.com")
        self.assertEqual(m["X-IBLS-RID"], "TEST0001")
        self.assertIn("https://survo.tech/s/TEST0001", m.get_body(("plain",)).get_content())
        self.assertEqual(dash("contact/TEST0001")["contact"]["status"], "invited")
        self.assertEqual(dash("contact/TEST0001")["contact"]["invite_msgid"], m["Message-ID"])

        # a second run finds nothing (already invited, reminders not due)
        again = send.MailboxSender("participate@survo.tech", api, "it-run-2", jitter=(0, 0), smtp_factory=FakeSMTP, sleep=lambda x: None).run()
        self.assertEqual(again["sent"], 0)

        # bounce for TEST0001 arrives via IMAP
        raw = DSN_HARD.replace(b"IBGONE22", b"TEST0001").replace(b"gone@acme.example", b"tester1@example.com")
        st = sync_mail.sync_mailbox(api, "participate@survo.tech", imap_factory=lambda: FakeIMAP({1: raw}), file_noise=False)
        self.assertEqual(st["posted"], 1)
        self.assertEqual(dash("contact/TEST0001")["contact"]["status"], "bounced")

        # TEST0002 completes the survey (Kobo)
        subs = {"results": [{"_id": 501, "rid": "TEST0002", "section_consent/consent": "1", "end": "2026-10-20T12:00:00+05:30"}], "next": None}
        os.environ.update({"KOBO_TOKEN": "x", "KOBO_ASSET_UID": "aTEST"})
        self.assertEqual(sync_kobo.main(api=api, opener=lambda req: FakeResp(json.dumps(subs).encode())), 0)
        self.assertEqual(dash("contact/TEST0002")["contact"]["status"], "completed")
        health = dash("summary")["health"]
        jobs = {j["job"] for j in health["jobs"]}
        self.assertIn("sync_kobo", jobs)


if __name__ == "__main__":
    unittest.main()
