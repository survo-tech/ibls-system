#!/usr/bin/env bash
# Full local end-to-end test: fresh local D1 -> migrations -> import all contacts -> seed test contacts
# -> wrangler dev --local -> node test/e2e.mjs -> stop.  Needs data/IBLS_outreach_tracker.xlsx (git-ignored).
set -euo pipefail
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}" no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}"
cd "$(dirname "$0")/.."
STATE=build/e2e-state
PORT=${PORT:-8788}
TRACKER=${TRACKER:-data/IBLS_outreach_tracker.xlsx}
[ -f .dev.vars ] || cp .dev.vars.example .dev.vars
if [ "${SKIP_SETUP:-0}" != "1" ]; then
  rm -rf "$STATE"
  npx wrangler d1 migrations apply ibls --local --persist-to "$STATE" >/dev/null
  python3 scripts/import_contacts.py --tracker "$TRACKER" --out build/import
  for f in build/import/*.sql; do npx wrangler d1 execute ibls --local --persist-to "$STATE" --file "$f" -y >/dev/null; done
  python3 scripts/seed_test.py --emails tester1@example.com,tester2@example.com --out build/seed_test.sql
  npx wrangler d1 execute ibls --local --persist-to "$STATE" --file build/seed_test.sql -y >/dev/null
fi
if curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then echo "port $PORT is already in use; stop the old dev server first" >&2; exit 1; fi
# Own process group so the whole wrangler/workerd tree is stopped on exit.
setsid npx wrangler dev --local --port "$PORT" --persist-to "$STATE" --test-scheduled > build/e2e-dev.log 2>&1 &
DEV=$!
trap 'kill -- -$DEV 2>/dev/null || true' EXIT
for i in $(seq 1 60); do curl -sf -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 1; done
# 1) Python jobs (send / sync_mail / sync_kobo) against the live local Worker, SMTP/IMAP/Kobo faked.
IBLS_BASE="http://127.0.0.1:$PORT" python3 -m unittest tests.integration_local -v
# 2) Reset the test contacts, then the HTTP end-to-end suite.
npx wrangler d1 execute ibls --local --persist-to "$STATE" --file build/seed_test.sql -y >/dev/null
BASE="http://127.0.0.1:$PORT" node --test test/e2e.mjs
