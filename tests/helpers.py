"""Shared fakes for the job tests (no network, no real SMTP/IMAP)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "jobs"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
os.environ.setdefault("API_TOKEN", "test-token-0123456789")
os.environ.setdefault("SMTP_PASS_PARTICIPATE", "pw-participate")
os.environ.setdefault("SMTP_PASS_RESEARCH", "pw-research")


class FakeApi:
    """Records calls; serves canned next-batch/state responses."""

    def __init__(self, batches=None, state=None, fail_events=False):
        self.batches = batches or {}
        self.state_store = dict(state or {})
        self.events_posted = []
        self.calls = []
        self.job_runs = []
        self.fail_events = fail_events

    def get(self, path, **params):
        self.calls.append(("GET", path, params))
        if path == "/api/next-batch":
            return self.batches.get(params["mailbox"], {"messages": [], "reason": "nothing due"})
        if path == "/api/summary":
            return self.summary
        raise AssertionError(path)

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        return {"ok": True}

    def put(self, path, body):
        self.calls.append(("PUT", path, body))
        return {"ok": True}

    def events(self, events):
        if self.fail_events:
            raise RuntimeError("API down")
        self.events_posted.extend(events)
        return {"accepted": len(events), "duplicate": 0, "rejected": 0}

    def job_run(self, job, ok, detail, **extra):
        self.job_runs.append((job, ok, detail, extra))

    def state(self, *keys):
        return {k: self.state_store[k] for k in keys if k in self.state_store}

    def set_state(self, **kv):
        self.state_store.update({k: str(v) for k, v in kv.items()})


class FakeSMTP:
    """Mimics smtplib.SMTP_SSL; `script` maps recipient -> exception to raise."""
    instances = []

    def __init__(self, script=None):
        self.script = script or {}
        self.sent = []
        self.logged_in = None
        self.closed = False
        FakeSMTP.instances.append(self)

    def login(self, user, pw):
        self.logged_in = (user, pw)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        rcpt = to_addrs[0]
        exc = self.script.get(rcpt)
        if exc is not None:
            if callable(exc) and not isinstance(exc, BaseException):
                exc = exc()
            raise exc
        self.sent.append(msg)
        return {}

    def quit(self):
        self.closed = True
