"""Shared helpers for the GitHub Actions jobs (stdlib only, plus Jinja2 for rendering)."""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("ibls")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

MAILBOXES = [m.strip().lower() for m in os.environ.get("MAILBOXES", "participate@survo.tech,research@survo.tech").split(",") if m.strip()]
SECRET_ENV_FOR = {"participate@survo.tech": "SMTP_PASS_PARTICIPATE", "research@survo.tech": "SMTP_PASS_RESEARCH"}


def iso(dt: _dt.datetime | None = None) -> str:
    """Canonical ISO-8601 UTC 'YYYY-MM-DDTHH:MM:SS.sssZ' (built field by field, matching the Worker)."""
    if dt is None:
        dt = _dt.datetime.fromtimestamp(time.time(), _dt.timezone.utc)
    d = dt.astimezone(_dt.timezone.utc)
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:{d.second:02d}.{d.microsecond // 1000:03d}Z"


def env(name: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        raise SystemExit(f"missing required environment variable {name}")
    return v or ""


def mailbox_password(mailbox: str) -> str:
    """SMTP/IMAP password for a mailbox: SMTP_PASS_PARTICIPATE / SMTP_PASS_RESEARCH."""
    name = SECRET_ENV_FOR.get(mailbox) or "SMTP_PASS_" + mailbox.split("@")[0].upper().replace("-", "_").replace(".", "_")
    return env(name, required=True)


class Api:
    """Tiny JSON client for the Worker's /api (Bearer API_TOKEN)."""

    def __init__(self, base: str | None = None, token: str | None = None, timeout: float = 30):
        self.base = (base or env("API_BASE", "https://survo.tech")).rstrip("/")
        self.token = token or env("API_TOKEN", required=True)
        self.timeout = timeout

    def _req(self, method: str, path: str, params: dict | None = None, body=None, retries: int = 3):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        last = None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": "Bearer " + self.token, "Content-Type": "application/json", "User-Agent": "ibls-jobs/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                detail = e.read()[:500].decode("utf-8", "replace")
                if e.code < 500:
                    raise RuntimeError(f"API {method} {path} -> {e.code}: {detail}") from None
                last = RuntimeError(f"API {method} {path} -> {e.code}: {detail}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"API {method} {path} failed after {retries} attempts: {last}")

    def get(self, path, **params):
        return self._req("GET", path, params=params or None)

    def post(self, path, body):
        return self._req("POST", path, body=body)

    def put(self, path, body):
        return self._req("PUT", path, body=body)

    # Convenience wrappers
    def events(self, events: list[dict]) -> dict:
        out = {"accepted": 0, "duplicate": 0, "rejected": 0}
        for i in range(0, len(events), 500):
            r = self.post("/api/events", {"events": events[i:i + 500]})
            for k in out:
                out[k] += r.get(k, 0)
        return out

    def job_run(self, job: str, ok: bool, detail, **extra):
        try:
            return self.post("/api/job-run", {"job": job, "ok": ok, "detail": detail, **extra})
        except Exception as e:  # heartbeat failure must not crash the job
            log.warning("job-run heartbeat failed: %s", e)

    def state(self, *keys: str) -> dict:
        return self.get("/api/state", keys=",".join(keys)).get("state", {})

    def set_state(self, **kv) -> None:
        self.put("/api/state", {k: str(v) for k, v in kv.items()})
