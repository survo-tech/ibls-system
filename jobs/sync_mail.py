#!/usr/bin/env python3
"""
Mailbox sync job (GitHub Actions, every 30 min).

For each mailbox: read new INBOX messages since the last processed UID (tracked in the Worker as state
`imap:<mailbox>` = "<uidvalidity>:<last_uid>"), classify them (bounce / complaint / auto-reply / opt-out / query)
and POST events. Messages are fetched with BODY.PEEK so their \\Seen flag is untouched (researchers still see
them as unread). Mail is never deleted. With FILE_NOISE=1, bounces / auto-replies / complaints are moved to
IBLS/Bounces, IBLS/Auto-replies, IBLS/Complaints (only if the server supports MOVE); queries stay in INBOX.

Environment: API_BASE, API_TOKEN, SMTP_PASS_PARTICIPATE, SMTP_PASS_RESEARCH (same Titan passwords),
IMAP_HOST (imap.titan.email), IMAP_SINCE (first-run start date, default 20-Oct-2026), MAX_MSGS (300), FILE_NOISE (0)
"""
from __future__ import annotations

import email.utils
import imaplib
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import MAILBOXES, Api, env, iso, log, mailbox_password  # noqa: E402
from mailclass import classify  # noqa: E402

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


def sync_mailbox(api: Api, mailbox: str, imap_factory=None, max_msgs: int | None = None, file_noise: bool | None = None) -> dict:
    max_msgs = max_msgs or int(env("MAX_MSGS", "300"))
    file_noise = env("FILE_NOISE", "0") == "1" if file_noise is None else file_noise
    imap = (imap_factory or (lambda: imaplib.IMAP4_SSL(env("IMAP_HOST", "imap.titan.email"), 993)))()
    stats = {"mailbox": mailbox, "fetched": 0, "posted": 0, "by_type": {}}
    try:
        imap.login(mailbox, mailbox_password(mailbox))
        imap.select("INBOX", readonly=not file_noise)
        uv = uidvalidity(imap)
        key = f"imap:{mailbox}"
        saved = api.state(key).get(key, "")
        last = 0
        if saved.startswith(uv + ":"):
            last = int(saved.split(":", 1)[1] or 0)
            typ, data = imap.uid("SEARCH", None, f"UID {last + 1}:*")
        else:
            since = env("IMAP_SINCE", "20-Oct-2026")
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


def main() -> int:
    api = Api()
    results, ok = [], True
    for mb in MAILBOXES:
        try:
            results.append(sync_mailbox(api, mb))
        except Exception as e:  # noqa: BLE001
            log.exception("[%s] sync failed", mb)
            results.append({"mailbox": mb, "error": str(e)[:300]})
            ok = False
    api.job_run("sync_mail", ok, {"results": results})
    log.info("done: %s", results)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
