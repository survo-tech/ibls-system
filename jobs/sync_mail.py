#!/usr/bin/env python3
"""
Mailbox sync job (GitHub Actions, every 30 min).

For each of the five mailboxes: read new INBOX mail, classify it (bounce / complaint / auto-reply / opt-out /
query, see mailclass.py) and POST events to the Worker. Mail is never deleted and never marked read.

Gmail API mode (default, MAIL_TRANSPORT=gmail_api): service account + domain-wide delegation impersonating each
mailbox. The cursor is state `gmail:<mailbox>` = {"history_id", "synced_at"}: new mail is read with
users.history.list (messageAdded, INBOX) since the stored historyId; on the first run, or when Gmail has expired
the history (404), with a users.messages.list query "in:inbox after:<epoch>". Messages are fetched with
format=raw. A reply without our headers is matched to its contact through the Gmail thread (our sent message in
the same thread carries X-IBLS-RID). With FILE_NOISE=1, bounces / auto-replies / complaints get an IBLS/* label
and leave the INBOX (archived, still unread); queries stay in the INBOX.

IMAP fallback (MAIL_TRANSPORT=smtp): imap.gmail.com:993 with the mailbox's app password. Cursor state
`imap:<mailbox>` = "<uidvalidity>:<last_uid>", BODY.PEEK fetches, IMAP MOVE for FILE_NOISE.

Environment: API_BASE, API_TOKEN, MAILBOXES, MAIL_TRANSPORT, GOOGLE_SA_JSON | GMAIL_APP_PW_<LOCALPART>,
MAIL_SINCE (first-run start date YYYY-MM-DD, default 2026-10-20), MAX_MSGS (500), FILE_NOISE (0), IMAP_HOST (imap.gmail.com)
"""
from __future__ import annotations

import datetime as _dt
import email.utils
import imaplib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import MAILBOXES, Api, app_password, env, iso, log, mail_transport  # noqa: E402
from gmail_api import SCOPES, GmailApiError, GmailClient, ServiceAccount, b64url_decode, header_value  # noqa: E402
from mailclass import RID_VALUE_RE, classify  # noqa: E402

NOISE_FOLDERS = {"bounced_hard": "IBLS/Bounces", "bounced_soft": "IBLS/Bounces", "dsn_delayed": "IBLS/Bounces",
                 "auto_reply": "IBLS/Auto-replies", "complaint": "IBLS/Complaints"}


def to_event(c: dict, mailbox: str, dedupe: str, ts: str) -> dict | None:
    """Map a classification to an /api/events payload (None = nothing to record)."""
    t = c["type"]
    if t in ("ignore", "dsn_delayed"):
        return None
    base = {"rid": c.get("rid"), "email": c.get("email"), "mailbox": mailbox, "ts": ts, "id": dedupe}
    meta = {"mailbox": mailbox, "message_id": c.get("message_id") or dedupe, "from": c.get("from"), "subject": c.get("subject"),
            "snippet": c.get("snippet"), **(c.get("detail") or {})}
    if t in ("bounced_hard", "bounced_soft"):
        # bounces are not "replies": keep from/subject out so they do not land in the replies inbox
        meta.pop("snippet", None)
        meta["dsn_from"] = meta.pop("from", None)
        meta["dsn_subject"] = meta.pop("subject", None)
        return {**base, "type": t, "meta": meta}
    if t == "complaint":
        return {**base, "type": "complaint", "meta": {**meta, "classification": "complaint"}}
    if t == "auto_reply":
        return {**base, "type": "auto_reply", "meta": {**meta, "classification": "auto_reply"}}
    if t == "opted_out":
        return {**base, "type": "opted_out", "meta": {**meta, "classification": "opted_out", "source": "email"}}
    return {**base, "type": "reply", "meta": {**meta, "classification": "query"}}


def msg_ts(raw: bytes, internaldate: bytes | None) -> str:
    if internaldate:
        m = re.search(rb'INTERNALDATE "([^"]+)"', internaldate)
        if m:
            try:
                return iso(email.utils.parsedate_to_datetime(_imap_date_to_rfc2822(m.group(1).decode())))
            except Exception:
                pass
    hm = re.search(rb"^Date:\s*(.+?)\r?$", raw, re.M | re.I)
    if hm:
        try:
            return iso(email.utils.parsedate_to_datetime(hm.group(1).decode("utf-8", "replace")))
        except Exception:
            pass
    return iso()


def _imap_date_to_rfc2822(s: str) -> str:
    # "20-Oct-2026 10:15:02 +0530" -> "20 Oct 2026 10:15:02 +0530"
    return s.strip().replace("-", " ", 2)


def uidvalidity(imap) -> str:
    typ, data = imap.response("UIDVALIDITY")
    if data and data[0]:
        return data[0].decode() if isinstance(data[0], bytes) else str(data[0])
    typ, data = imap.status("INBOX", "(UIDVALIDITY)")
    m = re.search(rb"UIDVALIDITY (\d+)", data[0] if data else b"")
    return m.group(1).decode() if m else "0"


def mail_since() -> _dt.date:
    return _dt.date.fromisoformat(env("MAIL_SINCE", "2026-10-20"))


def sync_mailbox(api: Api, mailbox: str, imap_factory=None, max_msgs: int | None = None, file_noise: bool | None = None) -> dict:
    """IMAP fallback (MAIL_TRANSPORT=smtp): imap.gmail.com with the mailbox's app password."""
    max_msgs = max_msgs or int(env("MAX_MSGS", "500"))
    file_noise = env("FILE_NOISE", "0") == "1" if file_noise is None else file_noise
    imap = (imap_factory or (lambda: imaplib.IMAP4_SSL(env("IMAP_HOST", "imap.gmail.com"), 993)))()
    stats = {"mailbox": mailbox, "transport": "imap", "fetched": 0, "posted": 0, "by_type": {}}
    try:
        imap.login(mailbox, app_password(mailbox))
        imap.select("INBOX", readonly=not file_noise)
        uv = uidvalidity(imap)
        key = f"imap:{mailbox}"
        saved = api.state(key).get(key, "")
        last = 0
        if saved.startswith(uv + ":"):
            last = int(saved.split(":", 1)[1] or 0)
            typ, data = imap.uid("SEARCH", None, f"UID {last + 1}:*")
        else:
            since = mail_since().strftime("%d-%b-%Y")
            typ, data = imap.uid("SEARCH", None, f"SINCE {since}")
        uids = sorted(int(u) for u in (data[0].split() if data and data[0] else []) if int(u) > last)[:max_msgs]
        events, moves, max_uid = [], [], last
        for uid in uids:
            typ, fd = imap.uid("FETCH", str(uid), "(BODY.PEEK[] INTERNALDATE)")
            raw, meta_line = None, None
            for item in fd or []:
                if isinstance(item, tuple):
                    meta_line, raw = item[0], item[1]
            if raw is None:
                continue
            stats["fetched"] += 1
            c = classify(raw, own_addresses=tuple(MAILBOXES))
            stats["by_type"][c["type"]] = stats["by_type"].get(c["type"], 0) + 1
            ev = to_event(c, mailbox, f"mail:{mailbox}:{uv}:{uid}", msg_ts(raw, meta_line))
            if ev:
                events.append(ev)
            if file_noise and c["type"] in NOISE_FOLDERS:
                moves.append((uid, NOISE_FOLDERS[c["type"]]))
            max_uid = max(max_uid, uid)
        if events:
            r = api.events(events)
            stats["posted"] = r.get("accepted", 0)
            stats["duplicate"] = r.get("duplicate", 0)
        if max_uid > last:
            api.set_state(**{key: f"{uv}:{max_uid}"})  # only after events were stored
        if moves:
            stats["moved"] = move_messages(imap, moves)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return stats


def move_messages(imap, moves: list[tuple[int, str]]) -> int:
    """UID MOVE when supported; otherwise leave mail where it is (never delete)."""
    caps = {c.decode() if isinstance(c, bytes) else str(c) for c in (getattr(imap, "capabilities", None) or ())}
    if "MOVE" not in {c.upper() for c in caps}:
        log.info("server lacks MOVE; leaving messages in INBOX")
        return 0
    n = 0
    for folder in sorted({f for _, f in moves}):
        imap.create(folder)  # harmless if it exists
        uids = ",".join(str(u) for u, f in moves if f == folder)
        typ, _ = imap.uid("MOVE", uids, folder)
        if typ == "OK":
            n += len(uids.split(","))
    return n


# --- Gmail API ---------------------------------------------------------------------------------------
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def _since_epoch(saved: dict) -> int:
    if saved.get("synced_at"):
        return max(0, int(saved["synced_at"]) - 3600)  # one hour of overlap; event ids make re-reads harmless
    d = mail_since()
    return int(_dt.datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())


def _new_ids_from_history(client: GmailClient, start: str, limit: int) -> tuple[list[str], str, bool]:
    """-> (message ids in arrival order, history id to store, truncated). Raises GmailApiError(404) if expired."""
    ids, seen, token, cursor = [], set(), None, None
    while True:
        r = client.list_history(start, page_token=token)
        for h in r.get("history", []) or []:
            added = [a.get("message", {}) for a in h.get("messagesAdded", []) or []]
            fresh = [m["id"] for m in added if m.get("id") and m["id"] not in seen and "INBOX" in (m.get("labelIds") or ["INBOX"])]
            if fresh and len(ids) + len(fresh) > limit:
                return ids, cursor or start, True  # stop at a record boundary; the rest is read next run
            for i in fresh:
                seen.add(i)
                ids.append(i)
            cursor = str(h.get("id") or cursor or "") or None
        token = r.get("nextPageToken")
        if not token:
            return ids, str(r.get("historyId") or cursor or start), False


def _ids_from_query(client: GmailClient, after_epoch: int, limit: int) -> tuple[list[str], bool]:
    """INBOX message ids received after `after_epoch`, oldest first."""
    ids, token = [], None
    while True:
        r = client.list_messages(f"in:inbox after:{after_epoch}", page_token=token)
        ids.extend(m["id"] for m in r.get("messages", []) or [])
        token = r.get("nextPageToken")
        if not token:
            break
    ids.reverse()  # the API lists newest first
    return ids[:limit], len(ids) > limit


def thread_rid(client: GmailClient, thread_id: str) -> str | None:
    """RID of our own message in the same Gmail thread (sent messages carry X-IBLS-RID)."""
    try:
        t = client.get_thread(thread_id, ["X-IBLS-RID"])
    except GmailApiError as e:
        log.info("[%s] thread lookup failed: %s", client.mailbox, e)
        return None
    for m in t.get("messages", []) or []:
        v = header_value(m.get("payload") or {}, "X-IBLS-RID")
        if v and RID_VALUE_RE.fullmatch(v.strip()):
            return v.strip().upper()
    return None


def _label_ids(client: GmailClient, names: set[str]) -> dict[str, str]:
    have = {lab.get("name"): lab.get("id") for lab in client.list_labels()}
    return {n: have.get(n) or client.create_label(n).get("id") for n in sorted(names)}


def sync_mailbox_gmail(api: Api, mailbox: str, client: GmailClient | None = None, max_msgs: int | None = None,
                       file_noise: bool | None = None) -> dict:
    """Gmail API mode: history (or query) since the last sync, format=raw fetch, classify, post, then advance."""
    max_msgs = max_msgs or int(env("MAX_MSGS", "500"))
    file_noise = env("FILE_NOISE", "0") == "1" if file_noise is None else file_noise
    client = client or GmailClient(mailbox, ServiceAccount.shared(), SCOPES)
    stats = {"mailbox": mailbox, "transport": "gmail_api", "fetched": 0, "posted": 0, "by_type": {}}
    key = f"gmail:{mailbox}"
    raw_state = api.state(key).get(key, "")
    try:
        saved = json.loads(raw_state) if raw_state else {}
    except ValueError:
        saved = {}
    # Read the mailbox's current historyId BEFORE listing, so nothing arriving meanwhile is skipped.
    profile_hist = str(client.profile().get("historyId") or "")
    new_hist: str | None
    if saved.get("history_id"):
        try:
            ids, new_hist, truncated = _new_ids_from_history(client, str(saved["history_id"]), max_msgs)
            stats["mode"] = "history"
        except GmailApiError as e:
            if e.status != 404:
                raise
            log.info("[%s] history %s expired; falling back to a query", mailbox, saved["history_id"])
            ids, truncated = _ids_from_query(client, _since_epoch(saved), max_msgs)
            new_hist = None if truncated else profile_hist
            stats["mode"] = "query (history expired)"
    else:
        ids, truncated = _ids_from_query(client, _since_epoch(saved), max_msgs)
        new_hist = None if truncated else profile_hist
        stats["mode"] = "query"

    events, relabel, last_ts = [], [], int(saved.get("synced_at") or 0)
    for mid in ids:
        try:
            m = client.get_message(mid, fmt="raw")
        except GmailApiError as e:
            if e.status == 404:  # deleted between listing and fetching
                continue
            raise
        internal = int(m.get("internalDate") or 0)
        last_ts = max(last_ts, internal // 1000)
        if "INBOX" not in (m.get("labelIds") or []):
            continue  # our own sent copies, drafts, mail already filed by a researcher
        raw = b64url_decode(m.get("raw", ""))
        stats["fetched"] += 1
        ts = iso(_dt.datetime.fromtimestamp(internal / 1000, _dt.timezone.utc)) if internal else msg_ts(raw, None)
        c = classify(raw, own_addresses=tuple(MAILBOXES))
        if not c.get("rid") and c["type"] in ("query", "opted_out", "auto_reply") and m.get("threadId"):
            c["rid"] = thread_rid(client, m["threadId"])
        stats["by_type"][c["type"]] = stats["by_type"].get(c["type"], 0) + 1
        ev = to_event(c, mailbox, f"mail:{mailbox}:gmail:{mid}", ts)
        if ev:
            events.append(ev)
        if file_noise and c["type"] in NOISE_FOLDERS:
            relabel.append((mid, NOISE_FOLDERS[c["type"]]))
    if events:
        r = api.events(events)  # raises if the Worker is unreachable: the cursor then stays where it was
        stats["posted"] = r.get("accepted", 0)
        stats["duplicate"] = r.get("duplicate", 0)
    # A truncated query run keeps no history id, so the next run continues by time from the last message read.
    api.set_state(**{key: json.dumps({"history_id": new_hist or "", "synced_at": last_ts})})
    if truncated:
        stats["truncated"] = True
    if relabel:
        label_for = _label_ids(client, {f for _, f in relabel})
        n = 0
        for mid, folder in relabel:
            try:
                client.modify(mid, add=[label_for[folder]], remove=["INBOX"])  # archive with a label; never read/delete
                n += 1
            except GmailApiError as e:
                log.warning("[%s] could not label %s: %s", mailbox, mid, e)
        stats["moved"] = n
    return stats


def main() -> int:
    api = Api()
    results, ok = [], True
    mode = mail_transport()
    if mode == "fake":
        # Fake mode exists for local send simulations; reading mail always needs a real mailbox (or a test fake).
        raise SystemExit("sync_mail does not support MAIL_TRANSPORT=fake; tests inject FakeGmailClient / a fake IMAP")
    for mb in MAILBOXES:
        try:
            results.append(sync_mailbox(api, mb) if mode == "smtp" else sync_mailbox_gmail(api, mb))
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] sync failed", mb)
            results.append({"mailbox": mb, "error": str(e)[:300]})
            ok = False
    api.job_run("sync_mail", ok, {"results": results})
    log.info("done: %s", results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
