"""
Classify inbound mail for the IBLS mailboxes (pure functions, unit-tested).

Result types:
  complaint     ARF feedback report (abuse / spam complaint)
  bounced_hard  DSN with 5.x.x status (or clear "user unknown" text from a mailer-daemon)
  bounced_soft  DSN with 4.x.x status / mailbox full
  dsn_delayed   "delayed" DSN: informational, not a bounce
  auto_reply    out-of-office and other automatic replies
  opted_out     a human asking to stop (OPT OUT, unsubscribe, remove, stop, not interested)
  query         anything else from a human: needs a researcher's attention
  ignore        our own outgoing copies
"""
from __future__ import annotations

import email
import re
from email import policy
from email.message import Message
from email.utils import getaddresses, parseaddr

RID_HEADER_RE = re.compile(r"^X-IBLS-RID:\s*([A-Z0-9]{4,16})\s*$", re.I | re.M)
MSGID_RID_RE = re.compile(r"<ibls\.([A-Z0-9]{4,16})\.[a-z0-9]+\.[0-9a-f]+@survo\.tech>", re.I)
STATUS_RE = re.compile(r"^Status:\s*([245])\.(\d{1,3})\.(\d{1,3})", re.I | re.M)
ACTION_RE = re.compile(r"^Action:\s*(\w+)", re.I | re.M)
FINAL_RCPT_RE = re.compile(r"^(?:Final|Original)-Recipient:\s*(?:rfc822;)?\s*<?([^\s>]+@[^\s>]+)>?", re.I | re.M)
ENH_CODE_RE = re.compile(r"\b([45])\.(\d{1,3})\.(\d{1,3})\b")
BOUNCE_FROM_RE = re.compile(r"mailer-daemon|postmaster|mail delivery (sub)?system|microsoftexchange", re.I)
BOUNCE_SUBJ_RE = re.compile(r"undeliver|delivery (status notification|failure|has failed|incomplete)|returned mail|failure notice|"
                            r"mail delivery failed|could not be delivered|non[- ]delivery|delivery problem", re.I)
HARD_TEXT_RE = re.compile(r"user unknown|no such user|does not exist|unknown user|mailbox (unavailable|not found)|recipient (rejected|not found|address rejected)|"
                          r"address (not found|rejected)|invalid recipient|no mailbox|account (disabled|has been disabled|is disabled)|domain (not found|does not exist)|"
                          r"host or domain name not found|NXDOMAIN|550[ -]", re.I)
SOFT_TEXT_RE = re.compile(r"mailbox (is )?full|over quota|quota exceeded|temporar|try again later|greylist|deferred", re.I)
AUTO_SUBJ_RE = re.compile(r"^\s*(automatic reply|auto(matic)?[- ]?reply|auto:|out of (the )?office|ooo\b|away from|on leave|on vacation|"
                          r"autosvar|abwesenheit|r[ée]ponse automatique|thank you for (your )?(e-?mail|message)|we have received your)", re.I)
OPTOUT_RE = re.compile(r"\b(opt[\s-]?out|unsubscribe|remove me|remove (us|our|my|this)|please remove|take me off|stop (emailing|sending|contacting)|"
                       r"^\s*stop\s*$|^\s*remove\s*$|not interested|do not (contact|email|e-mail|send)|don'?t (contact|email|send))\b", re.I | re.M)
QUOTE_START_RE = re.compile(r"^(>|On .{0,200}wrote:|-{2,}\s*Original Message|_{5,}|From:\s|Sent from my|\s*-----)", re.I | re.M)


def parse(raw: bytes) -> Message:
    return email.message_from_bytes(raw, policy=policy.compat32)


def header(msg: Message, name: str) -> str:
    v = msg.get(name)
    if v is None:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(str(v))))
    except Exception:
        return str(v)


def body_text(msg: Message) -> str:
    """First text/plain part (or tag-stripped text/html), decoded."""
    html = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if part.get("Content-Disposition", "").lower().startswith("attachment"):
            continue
        if ctype in ("text/plain", "text/html"):
            try:
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if ctype == "text/plain":
                return text
            html = html or re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(style|script).*?</\1>", " ", text))
    return html or ""


def new_text(text: str) -> str:
    """The part of a reply the sender actually wrote (drop the quoted original, which contains 'OPT OUT')."""
    m = QUOTE_START_RE.search(text)
    t = text[: m.start()] if m else text
    return re.sub(r"[ \t]+", " ", t).strip()


def find_rid(msg: Message, raw: bytes) -> str | None:
    """RID from our X-IBLS-RID header (embedded in bounces/ARF) or from In-Reply-To/References Message-IDs."""
    txt = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    m = RID_HEADER_RE.search(txt)
    if m:
        return m.group(1).upper()
    for h in ("In-Reply-To", "References"):
        m = MSGID_RID_RE.search(header(msg, h))
        if m:
            return m.group(1).upper()
    m = MSGID_RID_RE.search(txt)
    return m.group(1).upper() if m else None


def _report_type(msg: Message) -> str:
    if msg.get_content_type() == "multipart/report":
        return (msg.get_param("report-type") or "").lower()
    return ""


def _part_text(msg: Message, ctype: str) -> str:
    out = []
    for part in msg.walk():
        if part.get_content_type() == ctype:
            payload = part.get_payload()
            if isinstance(payload, list):  # message/delivery-status is parsed as a list of header blocks
                out.extend(str(p) for p in payload)
            else:
                dec = part.get_payload(decode=True)
                out.append(dec.decode("utf-8", "replace") if dec else str(payload))
    return "\n".join(out)


def classify(raw: bytes, own_addresses: tuple[str, ...] = ("participate@survo.tech", "research@survo.tech")) -> dict:
    msg = parse(raw)
    frm_name, frm = parseaddr(header(msg, "From"))
    frm = frm.lower()
    subject = header(msg, "Subject")
    rid = find_rid(msg, raw)
    out = {"type": "query", "rid": rid, "email": frm, "from": frm, "subject": subject[:300], "message_id": header(msg, "Message-ID").strip(),
           "snippet": "", "detail": {}}

    if frm in own_addresses and not BOUNCE_FROM_RE.search(frm_name or ""):
        out["type"] = "ignore"
        return out

    rtype = _report_type(msg)
    # 1. ARF complaint
    if rtype == "feedback-report" or msg.get_content_type() == "message/feedback-report" or _part_text(msg, "message/feedback-report"):
        fb = _part_text(msg, "message/feedback-report")
        m = re.search(r"^Original-Rcpt-To:\s*<?([^\s>]+)", fb, re.I | re.M)
        out.update(type="complaint", email=(m.group(1).lower() if m else None), detail={"feedback_type": _first(r"^Feedback-Type:\s*(\S+)", fb)})
        return out

    # 2. Bounces / DSNs
    is_dsn = rtype == "delivery-status" or bool(_part_text(msg, "message/delivery-status"))
    looks_bounce = is_dsn or BOUNCE_FROM_RE.search(frm) or BOUNCE_FROM_RE.search(frm_name or "") or (BOUNCE_SUBJ_RE.search(subject) and not header(msg, "In-Reply-To"))
    if looks_bounce:
        ds = _part_text(msg, "message/delivery-status") if is_dsn else ""
        text = ds or body_text(msg)
        recipient = _first(FINAL_RCPT_RE.pattern, ds or text, re.I | re.M)
        action = (_first(ACTION_RE.pattern, ds, re.I | re.M) or "").lower()
        sm = STATUS_RE.search(ds) if ds else None
        code = ".".join(sm.groups()) if sm else None
        if not code:
            em = ENH_CODE_RE.search(text)
            code = ".".join(em.groups()) if em else None
        diag = _first(r"^Diagnostic-Code:\s*(.+)$", ds, re.I | re.M) or ""
        out["email"] = recipient.lower() if recipient else None
        out["detail"] = {"status": code, "action": action, "diagnostic": diag[:300]}
        if action == "delayed":
            out["type"] = "dsn_delayed"
        elif code and code.startswith("5"):
            # 5.2.2 mailbox full is usually transient in practice: treat as soft
            out["type"] = "bounced_soft" if code.startswith("5.2.2") or SOFT_TEXT_RE.search(diag) else "bounced_hard"
        elif code and code.startswith("4"):
            out["type"] = "bounced_soft"
        elif HARD_TEXT_RE.search(text):
            out["type"] = "bounced_hard"
        elif SOFT_TEXT_RE.search(text):
            out["type"] = "bounced_soft"
        else:
            out["type"] = "bounced_soft" if is_dsn else "query"  # unknown mailer-daemon text: let a human look
        out["snippet"] = (diag or new_text(body_text(msg)))[:300]
        return out

    text = new_text(body_text(msg))
    out["snippet"] = text[:500]

    # 3. Automatic replies
    auto_sub = (msg.get("Auto-Submitted") or "").strip().lower()
    prec = (msg.get("Precedence") or "").strip().lower()
    is_auto = (
        (auto_sub and auto_sub != "no")
        or bool(msg.get("X-Autoreply")) or bool(msg.get("X-Autorespond"))
        or prec in ("auto_reply", "bulk", "junk", "list")
        or bool(AUTO_SUBJ_RE.search(subject))
    )
    if is_auto:
        out["type"] = "auto_reply"
        return out

    # 4. Opt-out requests (subject or the newly written text only, never the quoted invitation)
    if OPTOUT_RE.search(subject) or OPTOUT_RE.search(text[:600]):
        out["type"] = "opted_out"
        return out

    # 5. Everything else needs a human
    out["type"] = "query"
    return out


def _first(pattern: str, text: str, flags=0) -> str | None:
    m = re.search(pattern, text or "", flags)
    return m.group(1).strip() if m else None


def recipients(msg: Message) -> list[str]:
    return [a.lower() for _, a in getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []))]
