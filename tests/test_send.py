import smtplib
import unittest

from tests.helpers import FakeApi, FakeSMTP  # noqa: F401 (sets sys.path)
import render  # noqa: E402
import send  # noqa: E402

SETTINGS = {"postal_address": "", "from_name": "India Business Leaders Survey", "test_mode": False, "test_recipients": []}


def msg(rid="IBAAAA22", email="ceo@acme.example", kind="invite", **kw):
    d = {"rid": rid, "email": email, "salutation": "Dear Mr Rao,", "company": "Acme Engineering Private Limited",
         "peer_tier": "over ₹100 crore manufacturing", "mailbox": "participate@survo.tech", "kind": kind, "invite_msgid": None,
         "survey_link": f"https://survo.tech/s/{rid}", "optout_link": f"https://survo.tech/optout/{rid}"}
    d.update(kw)
    return d


def sender(api, smtp, **kw):
    opts = dict(jitter=(0, 0), budget=600, max_per_run=20, smtp_factory=lambda: smtp, sleep=lambda s: None)
    opts.update(kw)
    return send.MailboxSender("participate@survo.tech", api, "run-1", **opts)


class RenderTests(unittest.TestCase):
    def test_invite_text_exact(self):
        subject, text, html = render.render("invite", {k: msg()[k] for k in ("salutation", "company", "peer_tier", "survey_link", "optout_link")})
        self.assertEqual(subject, "Research invitation: India Business Leaders Survey for Acme Engineering Private Limited")
        self.assertTrue(text.startswith("Dear Mr Rao,\n\nOn behalf of researchers at London Business School, MIT and Harvard University"))
        self.assertIn("other firms in the over ₹100 crore manufacturing segment.", text)
        self.assertIn("Start the survey: https://survo.tech/s/IBAAAA22\n(about 15–20 minutes · confidential · voluntary)", text)
        self.assertIn("You can verify this study at https://survo.tech or by writing to ethics@london.edu quoting REC1101.", text)
        self.assertIn("With thanks,\nSukrit Singh Puri, London Business School\nVishan Nigam and Aroon Narayanan, MIT\n"
                      "Shivram Viswanathan, Harvard University\nFieldwork: Morsel Research and Development Pvt Ltd · participate@survo.tech", text)
        self.assertIn('To stop further emails, reply "OPT OUT" or visit https://survo.tech/optout/IBAAAA22.', text)
        self.assertTrue(text.rstrip().endswith("https://survo.tech/optout/IBAAAA22."))  # empty postal address adds nothing
        # HTML: one button, no images, escaped company
        self.assertEqual(html.count(">Start the survey</a>"), 1)
        self.assertNotIn("<img", html)

    def test_reminder_and_postal_address(self):
        ctx = {k: msg()[k] for k in ("salutation", "company", "peer_tier", "survey_link", "optout_link")}
        ctx["postal_address"] = "Morsel R&D, Lucknow 226022"
        subject, text, html = render.render("reminder", ctx)
        self.assertEqual(subject, "Reminder: India Business Leaders Survey for Acme Engineering Private Limited")
        self.assertIn("A short reminder of our invitation to Acme Engineering Private Limited to take part", text)
        self.assertIn("please feel free to forward this email. If you have already responded, thank you, and please ignore this note.", text)
        self.assertTrue(text.rstrip().endswith("Morsel R&D, Lucknow 226022"))
        self.assertIn("Morsel R&amp;D", html)

    def test_html_escapes(self):
        ctx = {k: msg()[k] for k in ("salutation", "company", "peer_tier", "survey_link", "optout_link")}
        ctx["company"] = "A <b>&</b> B Ltd"
        _, text, html = render.render("invite", ctx)
        self.assertIn("A <b>&</b> B Ltd", text)
        self.assertIn("A &lt;b&gt;&amp;&lt;/b&gt; B Ltd", html)

    def test_missing_variable(self):
        with self.assertRaises(ValueError):
            render.render("invite", {"salutation": "x"})


class BuildMessageTests(unittest.TestCase):
    def test_headers(self):
        m = send.build_message(msg(), SETTINGS, "research@survo.tech")
        self.assertEqual(m["To"], "ceo@acme.example")
        self.assertIn("research@survo.tech", m["From"])
        self.assertEqual(m["Reply-To"], "participate@survo.tech")
        self.assertEqual(m["List-Unsubscribe"], "<mailto:participate@survo.tech?subject=OPT%20OUT>, <https://survo.tech/optout/IBAAAA22>")
        self.assertEqual(m["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertEqual(m["X-IBLS-RID"], "IBAAAA22")
        self.assertRegex(m["Message-ID"], r"^<ibls\.IBAAAA22\.invite\.[0-9a-f]+@survo\.tech>$")
        self.assertTrue(m.is_multipart())
        self.assertEqual([p.get_content_type() for p in m.iter_parts()], ["text/plain", "text/html"])
        self.assertFalse(any(p.get_content_disposition() == "attachment" for p in m.walk()))

    def test_reminder_threads_to_invite(self):
        m = send.build_message(msg(kind="reminder", invite_msgid="<ibls.IBAAAA22.invite.abc@survo.tech>"), SETTINGS, "participate@survo.tech")
        self.assertEqual(m["In-Reply-To"], "<ibls.IBAAAA22.invite.abc@survo.tech>")
        self.assertTrue(m["Subject"].startswith("Reminder:"))


class SenderTests(unittest.TestCase):
    def test_sends_and_posts_events(self):
        api = FakeApi(batches={"participate@survo.tech": {"messages": [msg("IBAAAA22", "a@x.example"), msg("IBBBBB33", "b@x.example", kind="reminder")],
                                                          "settings": SETTINGS, "run_id": "run-1:participate@survo.tech"}})
        smtp = FakeSMTP()
        st = sender(api, smtp).run()
        self.assertEqual(st["sent"], 2)
        self.assertEqual(len(smtp.sent), 2)
        self.assertEqual(smtp.logged_in, ("participate@survo.tech", "pw-participate"))
        types = [(e["type"], e["kind"], e["rid"]) for e in api.events_posted]
        self.assertEqual(types, [("sent", "invite", "IBAAAA22"), ("sent", "reminder", "IBBBBB33")])
        self.assertTrue(all(e["id"].startswith("sent:<ibls.") for e in api.events_posted))
        self.assertIn(("POST", "/api/release", {"run_id": "run-1:participate@survo.tech"}), api.calls)
        self.assertTrue(smtp.closed)

    def test_hard_reject_and_soft_fail(self):
        script = {
            "bad@x.example": smtplib.SMTPRecipientsRefused({"bad@x.example": (550, b"5.1.1 User unknown")}),
            "tmp@x.example": smtplib.SMTPRecipientsRefused({"tmp@x.example": (451, b"4.3.0 Try later")}),
        }
        api = FakeApi(batches={"participate@survo.tech": {"messages": [msg("IBAAAA22", "bad@x.example"), msg("IBBBBB33", "tmp@x.example"), msg("IBCCCC44", "ok@x.example")], "settings": SETTINGS}})
        st = sender(api, FakeSMTP(script)).run()
        self.assertEqual((st["sent"], st["hard"], st["failed"]), (1, 1, 1))
        kinds = [(e["type"], e.get("kind")) for e in api.events_posted]
        self.assertEqual(kinds, [("bounced_hard", "smtp"), ("failed", "invite"), ("sent", "invite")])

    def test_quota_stops_mailbox(self):
        script = {"b@x.example": smtplib.SMTPDataError(550, b"5.4.6 Daily sending quota exceeded")}
        api = FakeApi(batches={"participate@survo.tech": {"messages": [msg("IBAAAA22", "a@x.example"), msg("IBBBBB33", "b@x.example"), msg("IBCCCC44", "c@x.example")], "settings": SETTINGS}})
        st = sender(api, FakeSMTP(script)).run()
        self.assertTrue(st["quota"])
        self.assertEqual(st["sent"], 1)
        self.assertEqual([e["type"] for e in api.events_posted], ["sent", "failed"])  # c@ never attempted

    def test_reconnects_once_on_disconnect(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                return smtplib.SMTPServerDisconnected("gone")
            return None

        class Flaky(FakeSMTP):
            def send_message(self, m, from_addr=None, to_addrs=None):
                e = flaky()
                if e:
                    raise e
                self.sent.append(m)

        smtps = []
        api = FakeApi(batches={"participate@survo.tech": {"messages": [msg()], "settings": SETTINGS}})

        def factory():
            s = Flaky()
            smtps.append(s)
            return s
        st = sender(api, None, smtp_factory=factory).run()
        self.assertEqual(st["sent"], 1)
        self.assertEqual(len(smtps), 2)

    def test_test_mode_guard(self):
        s = dict(SETTINGS, test_mode=True, test_recipients=["me@test.example"])
        api = FakeApi(batches={"participate@survo.tech": {"messages": [msg(email="real@company.example")], "settings": s, "run_id": "r"}})
        smtp = FakeSMTP()
        st = sender(api, smtp).run()
        self.assertEqual(len(smtp.sent), 0)
        self.assertIn("test_mode", st["error"])

    def test_budget_limits_sends(self):
        clock = {"t": 0.0}
        api = FakeApi(batches={"participate@survo.tech": {"messages": [msg(f"IBAAA{i}22", f"{i}@x.example") for i in range(5)], "settings": SETTINGS}})
        smtp = FakeSMTP()

        def sleep(s):
            clock["t"] += s
        st = sender(api, smtp, jitter=(40, 40), budget=100, sleep=sleep, clock=lambda: clock["t"]).run()
        self.assertEqual(st["sent"], 3)  # t=0, 40, 80; next would end at 120 > 100
        self.assertEqual(st["reason"], "run time budget reached")

    def test_planned_max(self):
        s = sender(FakeApi(), FakeSMTP(), jitter=(20, 60), budget=540, max_per_run=20)
        self.assertEqual(s.planned_max(), 14)

    def test_stops_if_events_cannot_be_recorded(self):
        api = FakeApi(batches={"participate@survo.tech": {"messages": [msg("IBAAAA22", "a@x.example"), msg("IBBBBB33", "b@x.example")], "settings": SETTINGS}}, fail_events=True)
        smtp = FakeSMTP()
        st = sender(api, smtp).run()
        self.assertEqual(len(smtp.sent), 1)
        self.assertIn("could not record", st["error"])

    def test_dry_run_sends_nothing(self):
        api = FakeApi(batches={"participate@survo.tech": {"messages": [msg()], "settings": SETTINGS}})
        smtp = FakeSMTP()
        st = sender(api, smtp, dry_run=True).run()
        self.assertEqual((len(smtp.sent), st["skipped"], len(api.events_posted)), (0, 1, 0))

    def test_classify(self):
        self.assertEqual(send.classify_smtp_error(smtplib.SMTPAuthenticationError(535, b"bad"))[0], "auth")
        self.assertEqual(send.classify_smtp_error(smtplib.SMTPSenderRefused(451, b"4.7.1 rate limit exceeded", "x"))[0], "quota")
        self.assertEqual(send.classify_smtp_error(smtplib.SMTPDataError(554, b"5.7.1 content rejected"))[0], "soft")


if __name__ == "__main__":
    unittest.main()
