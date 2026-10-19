import io
import json
import os
import random
import unittest
from unittest import mock

from tests.helpers import FakeApi  # noqa: F401 (sets sys.path)
import daily_report  # noqa: E402
import import_contacts  # noqa: E402
import seed_test  # noqa: E402
import sync_kobo  # noqa: E402

SUBS = [
    {"_id": 11, "rid": "IBAAAA22", "section_consent/consent": "1", "end": "2026-10-28T11:45:12.000+05:30", "_uuid": "u1"},
    {"_id": 12, "rid": "IBBBBB33", "section_consent/consent": "0", "end": "2026-10-28T12:00:00.000+05:30"},
    {"_id": 13, "section_consent/consent": "1", "end": "2026-10-28T12:30:00.000+05:30"},  # organic (no rid)
    {"_id": 14, "rid": "ib-bad!", "section_consent/consent": "1", "_submission_time": "2026-10-28T08:00:00"},  # bad rid, no end
]


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class KoboTests(unittest.TestCase):
    def test_submission_mapping(self):
        e = sync_kobo.submission_events(SUBS[0])
        self.assertEqual([(x["type"], x["rid"], x["id"]) for x in e], [("started", "IBAAAA22", "kobo:11:started"), ("completed", "IBAAAA22", "kobo:11:completed")])
        self.assertEqual(e[0]["ts"], "2026-10-28T06:15:12.000Z")
        self.assertEqual([x["type"] for x in sync_kobo.submission_events(SUBS[1])], ["started", "consent_declined"])
        org = sync_kobo.submission_events(SUBS[2])
        self.assertEqual({(x["rid"], x["kind"]) for x in org}, {(None, "organic")})
        bad = sync_kobo.submission_events(SUBS[3])
        self.assertEqual([x["type"] for x in bad], ["started"])  # no end -> not completed
        self.assertEqual(bad[0]["meta"]["rid_raw"], "IB-BAD!")
        self.assertEqual(bad[0]["ts"], "2026-10-28T08:00:00.000Z")

    def test_grouped_rid_field(self):
        e = sync_kobo.submission_events({"_id": 1, "grp/rid": "IBCCCC44", "section_consent/consent": "1", "end": "2026-10-28T10:00:00+05:30"})
        self.assertEqual(e[0]["rid"], "IBCCCC44")

    def test_main_incremental_with_pagination(self):
        pages = {
            1: {"results": SUBS[:2], "next": "https://kf.kobotoolbox.org/page2"},
            2: {"results": SUBS[2:], "next": None},
        }
        seen = []

        def opener(req):
            seen.append(req)
            return FakeResp(json.dumps(pages[len(seen)]).encode())

        api = FakeApi(state={"kobo_last_id": "10"})
        with mock.patch.dict(os.environ, {"KOBO_TOKEN": "kt", "KOBO_ASSET_UID": "aABC"}):
            rc = sync_kobo.main(api=api, opener=opener)
        self.assertEqual(rc, 0)
        self.assertEqual(seen[0].get_header("Authorization"), "Token kt")
        self.assertIn("/api/v2/assets/aABC/data/", seen[0].full_url)
        self.assertIn("%24gt%22%3A+10", seen[0].full_url)  # query={"_id":{"$gt":10}}
        self.assertEqual(api.state_store["kobo_last_id"], "14")
        self.assertEqual(len(api.events_posted), 7)
        self.assertEqual(api.job_runs[-1][0:2], ("sync_kobo", True))


class DailyReportTests(unittest.TestCase):
    def test_compose(self):
        s = {"day": "2026-10-28", "paused": True, "pause_reason": "Auto-pause: 3 complaints", "test_mode": False,
             "today": {"invites": 80, "reminders": 0, "failed": 1, "hard": 2, "soft": 1, "clicks": 9, "started": 4, "completed": 3,
                       "declined": 1, "opted_out": 2, "complaints": 0, "replies": 3},
             "totals": {"invited": 80, "sendable": 7969, "completed": 3, "bounced_hard": 2, "opted_out": 2, "complaints": 0, "replies_open": 3},
             "jobs": [{"job": "send", "ts": "2026-10-28T11:20:00.000Z", "ok": 1}]}
        subject, body = daily_report.compose(s, "https://survo.tech/dashboard")
        self.assertIn("80 invites", subject)
        self.assertIn("[PAUSED]", subject)
        self.assertIn("PAUSED: Auto-pause: 3 complaints", body)
        self.assertIn("Replies needing attention: 3", body)
        self.assertIn("2.5%", body)  # 2 hard bounces of 80


class ImportTests(unittest.TestCase):
    def rows(self, n=400):
        rng = random.Random(1)
        out = []
        for i in range(n):
            out.append({"rid": f"IBX{i:05d}", "email": f"p{i}@x.example", "salutation": "Dear Director,", "director_name": "", "company": f"Co {i}",
                        "cin": "", "state": "", "region": rng.choice(["North", "South", "East", "West"]), "size_band": rng.choice(["<10cr", "10-100cr", ">100cr"]),
                        "nature": rng.choice(["Manufacturing", "Non-Manufacturing"]), "peer_tier": "x", "email_tier": rng.choice(["T1", "T2"]),
                        "status": "on_hold" if i % 50 == 0 else "queued", "notes": ""})
        return out

    def test_order_is_balanced_and_mailboxes_alternate(self):
        rows = self.rows()
        import_contacts.assign_order(rows, random.Random(7))
        q = sorted([r for r in rows if r["status"] == "queued"], key=lambda r: r["send_order"])
        hold = [r for r in rows if r["status"] == "on_hold"]
        self.assertTrue(max(r["send_order"] for r in q) < min(r["send_order"] for r in hold))
        self.assertEqual([r["mailbox"] for r in q[:4]], ["participate@survo.tech", "research@survo.tech"] * 2)
        # Balance: the first 25% of the order holds roughly 25% of each region.
        first = q[: len(q) // 4]
        for reg in ("North", "South", "East", "West"):
            share = sum(r["region"] == reg for r in first) / max(1, sum(r["region"] == reg for r in q))
            self.assertAlmostEqual(share, 0.25, delta=0.08)

    def test_sql_is_upsert_and_escaped(self):
        rows = self.rows(3)
        rows[0]["company"] = "O'Brien & Sons"
        import_contacts.assign_order(rows, random.Random(1))
        sql = "\n".join(import_contacts.upsert_sql(rows))
        self.assertIn("'O''Brien & Sons'", sql)
        self.assertIn("ON CONFLICT(rid) DO UPDATE", sql)
        self.assertNotIn("status=excluded.status", sql)  # re-import never resets progress

    def test_validate_catches_duplicates(self):
        rows = self.rows(3)
        rows[1]["email"] = rows[0]["email"]
        self.assertTrue(any("duplicate email" in p for p in import_contacts.validate(rows)))

    def test_seed_sql(self):
        sql = seed_test.build_sql(["a@test.example", "b@test.example"])
        self.assertIn("'TEST0001'", sql)
        self.assertIn("'research@survo.tech'", sql)
        self.assertIn("('test_mode', 'true')", sql)
        self.assertIn("'a@test.example,b@test.example'", sql)


if __name__ == "__main__":
    unittest.main()
