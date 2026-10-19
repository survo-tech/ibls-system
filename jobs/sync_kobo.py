#!/usr/bin/env python3
"""
KoboToolbox sync job (GitHub Actions, every 30 min).

Reads new submissions from the Kobo API v2 (incrementally by _id; cursor kept in Worker state `kobo_last_id`)
and posts events:
  started           every submission (Kobo only stores submitted forms, so "started" = submitted anything)
  completed         consent agreed ('1') and the submission has an `end` time
  consent_declined  consent '0'
The hidden field `rid` (prefilled from https://survo.tech/s/<RID> via ?d[rid]=) links a submission to a contact.
Submissions without a rid are recorded with kind='organic' (counted separately on the dashboard).

Environment: API_BASE, API_TOKEN, KOBO_TOKEN, KOBO_ASSET_UID, KOBO_API_BASE (https://kf.kobotoolbox.org),
KOBO_RID_FIELD (rid), KOBO_CONSENT_FIELD (section_consent/consent)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Api, env, iso, log  # noqa: E402

RID_RE = re.compile(r"^[A-Z0-9]{4,16}$")


def field(sub: dict, name: str):
    """Value of a question whether stored top-level ('rid') or inside groups ('grp/rid')."""
    if name in sub:
        return sub[name]
    leaf = name.split("/")[-1]
    for k, v in sub.items():
        if k == leaf or k.endswith("/" + leaf):
            return v
    return None


def kobo_ts(sub: dict) -> str:
    for k in ("end", "_submission_time", "start"):
        v = sub.get(k)
        if v:
            try:
                s = str(v)
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                d = dt.datetime.fromisoformat(s)
                if d.tzinfo is None:  # _submission_time is UTC without offset
                    d = d.replace(tzinfo=dt.timezone.utc)
                return iso(d)
            except ValueError:
                continue
    return iso()


def submission_events(sub: dict, rid_field="rid", consent_field="section_consent/consent") -> list[dict]:
    sid = sub.get("_id")
    raw_rid = str(field(sub, rid_field) or "").strip().upper()
    rid = raw_rid if RID_RE.match(raw_rid) else None
    consent = str(field(sub, consent_field) or "").strip()
    ts = kobo_ts(sub)
    kind = None if rid else "organic"
    meta = {"submission_id": sid, "uuid": sub.get("_uuid"), "consent": consent}
    if raw_rid and not rid:
        meta["rid_raw"] = raw_rid[:40]
    base = {"rid": rid, "kind": kind, "ts": ts, "meta": meta}
    evs = [{**base, "type": "started", "id": f"kobo:{sid}:started"}]
    if consent == "1" and sub.get("end"):
        evs.append({**base, "type": "completed", "id": f"kobo:{sid}:completed"})
    elif consent == "0":
        evs.append({**base, "type": "consent_declined", "id": f"kobo:{sid}:declined"})
    return evs


def fetch_new(last_id: int, opener=None, page_size: int = 500):
    """Yield submissions with _id > last_id in ascending order."""
    base = env("KOBO_API_BASE", "https://kf.kobotoolbox.org").rstrip("/")
    asset = env("KOBO_ASSET_UID", required=True)
    token = env("KOBO_TOKEN", required=True)
    params = {"format": "json", "limit": str(page_size), "sort": json.dumps({"_id": 1}), "query": json.dumps({"_id": {"$gt": last_id}})}
    url = f"{base}/api/v2/assets/{asset}/data/?{urllib.parse.urlencode(params)}"
    open_ = opener or (lambda req: urllib.request.urlopen(req, timeout=60))
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Token {token}", "Accept": "application/json"})
        with open_(req) as r:
            body = json.loads(r.read())
        for sub in body.get("results", []):
            yield sub
        url = body.get("next")


def main(api: Api | None = None, opener=None) -> int:
    api = api or Api()
    try:
        last = int(api.state("kobo_last_id").get("kobo_last_id") or 0)
        events, max_id, n = [], last, 0
        rid_f = env("KOBO_RID_FIELD", "rid")
        consent_f = env("KOBO_CONSENT_FIELD", "section_consent/consent")
        for sub in fetch_new(last, opener=opener):
            n += 1
            events.extend(submission_events(sub, rid_f, consent_f))
            max_id = max(max_id, int(sub.get("_id") or 0))
        res = api.events(events) if events else {"accepted": 0}
        if max_id > last:
            api.set_state(kobo_last_id=max_id)
        detail = {"submissions": n, "events": len(events), "accepted": res.get("accepted", 0), "last_id": max_id}
        api.job_run("sync_kobo", True, detail)
        log.info("kobo sync: %s", detail)
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("kobo sync failed")
        api.job_run("sync_kobo", False, str(e)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())
