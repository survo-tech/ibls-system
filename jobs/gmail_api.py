"""
Minimal Gmail API client for the IBLS jobs (no Google client libraries).

Authentication: a Google Cloud service account with domain-wide delegation (DWD). For each mailbox the job
signs a JWT (RS256) whose `sub` is the mailbox, exchanges it at the OAuth token endpoint and calls the Gmail
REST API as that user. Credentials come from the GOOGLE_SA_JSON secret (the service account's JSON key) or a
file named by GOOGLE_SA_FILE.

Every network call goes through an injectable `http(method, url, headers, body) -> (status, bytes)` so the
tests never touch the network.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URI = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
# The DWD client must be authorised for exactly these three scopes in the Admin console.
SCOPES = (SCOPE_SEND, SCOPE_READONLY, SCOPE_MODIFY)


class GmailApiError(Exception):
    """An error response from the token endpoint or the Gmail API."""

    def __init__(self, status: int, reason: str, message: str, token_error: bool = False):
        super().__init__(f"{status} {reason}: {message}")
        self.status, self.reason, self.message, self.token_error = status, reason, message, token_error


def urllib_http(method: str, url: str, headers: dict, body: bytes | None, timeout: float = 60) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def parse_error(status: int, body: bytes, token_error: bool = False) -> GmailApiError:
    try:
        j = json.loads(body or b"{}")
    except ValueError:
        return GmailApiError(status, "http_error", body[:300].decode("utf-8", "replace"), token_error)
    err = j.get("error")
    if isinstance(err, str):  # OAuth token endpoint: {"error": "unauthorized_client", "error_description": "..."}
        return GmailApiError(status, err, str(j.get("error_description", "")), True)
    if isinstance(err, dict):  # Google API: {"error": {"code", "message", "status", "errors": [{"reason"}]}}
        reasons = [e.get("reason") for e in err.get("errors") or [] if e.get("reason")]
        return GmailApiError(status, reasons[0] if reasons else str(err.get("status", "error")), str(err.get("message", "")), token_error)
    return GmailApiError(status, "http_error", str(j)[:300], token_error)


class ServiceAccount:
    """Service-account key + per-(subject, scopes) access-token cache (thread-safe)."""

    _shared = None
    _shared_lock = threading.Lock()

    def __init__(self, info: dict, http=None, clock=time.time):
        missing = [k for k in ("client_email", "private_key") if not info.get(k)]
        if missing:
            raise ValueError(f"service account JSON lacks {missing}")
        self.email = info["client_email"]
        self.private_key = info["private_key"]
        self.key_id = info.get("private_key_id")
        self.token_uri = info.get("token_uri") or TOKEN_URI
        self.http = http or urllib_http
        self.clock = clock
        self._cache: dict[tuple, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._signer = None

    @classmethod
    def from_env(cls, http=None) -> "ServiceAccount":
        raw = os.environ.get("GOOGLE_SA_JSON", "").strip()
        if not raw and os.environ.get("GOOGLE_SA_FILE"):
            with open(os.environ["GOOGLE_SA_FILE"], encoding="utf-8") as f:
                raw = f.read()
        if not raw:
            raise SystemExit("missing GOOGLE_SA_JSON (service account key JSON) for MAIL_TRANSPORT=gmail_api")
        if not raw.startswith("{"):  # allow a base64-encoded key as well
            raw = base64.b64decode(raw).decode("utf-8")
        return cls(json.loads(raw), http=http)

    @classmethod
    def shared(cls) -> "ServiceAccount":
        with cls._shared_lock:
            if cls._shared is None:
                cls._shared = cls.from_env()
            return cls._shared

    def _sign(self, data: bytes) -> bytes:
        if self._signer is None:
            from cryptography.hazmat.primitives import serialization  # imported lazily: only gmail_api mode needs it
            self._signer = serialization.load_pem_private_key(self.private_key.encode(), password=None)
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        return self._signer.sign(data, padding.PKCS1v15(), hashes.SHA256())

    def assertion(self, subject: str, scopes: tuple[str, ...]) -> str:
        now = int(self.clock())
        header = {"alg": "RS256", "typ": "JWT"}
        if self.key_id:
            header["kid"] = self.key_id
        claims = {"iss": self.email, "sub": subject, "scope": " ".join(scopes), "aud": self.token_uri, "iat": now, "exp": now + 3600}
        signing_input = b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + b64url(json.dumps(claims, separators=(",", ":")).encode())
        return signing_input + "." + b64url(self._sign(signing_input.encode()))

    def token(self, subject: str, scopes: tuple[str, ...] = SCOPES, force: bool = False) -> str:
        k = (subject.lower(), tuple(scopes))
        with self._lock:
            hit = self._cache.get(k)
            if hit and not force and hit[1] - 60 > self.clock():
                return hit[0]
        body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": self.assertion(subject, scopes)}).encode()
        status, data = self.http("POST", self.token_uri, {"Content-Type": "application/x-www-form-urlencoded"}, body)
        if status != 200:
            raise parse_error(status, data, token_error=True)
        j = json.loads(data)
        tok = j["access_token"]
        with self._lock:
            self._cache[k] = (tok, self.clock() + float(j.get("expires_in", 3600)))
        return tok


class GmailClient:
    """Gmail REST calls as one mailbox (users/me = the impersonated user)."""

    def __init__(self, mailbox: str, sa: ServiceAccount | None = None, scopes: tuple[str, ...] = SCOPES, http=None, base: str = API_BASE):
        self.mailbox = mailbox
        self.sa = sa or ServiceAccount.shared()
        self.scopes = tuple(scopes)
        self.http = http or self.sa.http
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in (1, 2):
            headers = {"Authorization": "Bearer " + self.sa.token(self.mailbox, self.scopes, force=attempt == 2), "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            status, resp = self.http(method, url, headers, data)
            if status == 401 and attempt == 1:
                continue  # expired / revoked token: fetch a new one once
            if status >= 400:
                raise parse_error(status, resp)
            return json.loads(resp) if resp else {}
        raise GmailApiError(401, "unauthorized", "token refused twice")

    # --- endpoints used by the jobs -------------------------------------------------------------
    def send_raw(self, raw: bytes, thread_id: str | None = None) -> dict:
        body = {"raw": b64url(raw)}
        if thread_id:
            body["threadId"] = thread_id
        return self.call("POST", "/messages/send", body=body)

    def get_message(self, msg_id: str, fmt: str = "raw", headers: list[str] | None = None) -> dict:
        params: dict = {"format": fmt}
        if headers:
            params["metadataHeaders"] = headers
        return self.call("GET", f"/messages/{urllib.parse.quote(msg_id)}", params=params)

    def list_messages(self, q: str, page_token: str | None = None, max_results: int = 100) -> dict:
        params = {"q": q, "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        return self.call("GET", "/messages", params=params)

    def list_history(self, start_history_id: str, page_token: str | None = None) -> dict:
        params = {"startHistoryId": start_history_id, "historyTypes": "messageAdded", "labelId": "INBOX", "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        return self.call("GET", "/history", params=params)

    def profile(self) -> dict:
        return self.call("GET", "/profile")

    def get_thread(self, thread_id: str, headers: list[str]) -> dict:
        return self.call("GET", f"/threads/{urllib.parse.quote(thread_id)}", params={"format": "metadata", "metadataHeaders": headers})

    def list_labels(self) -> list[dict]:
        return self.call("GET", "/labels").get("labels", [])

    def create_label(self, name: str) -> dict:
        return self.call("POST", "/labels", body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"})

    def modify(self, msg_id: str, add: list[str] | None = None, remove: list[str] | None = None) -> dict:
        return self.call("POST", f"/messages/{urllib.parse.quote(msg_id)}/modify", body={"addLabelIds": add or [], "removeLabelIds": remove or []})


def header_value(payload: dict, name: str) -> str | None:
    for h in (payload or {}).get("headers", []) or []:
        if str(h.get("name", "")).lower() == name.lower():
            return h.get("value")
    return None
