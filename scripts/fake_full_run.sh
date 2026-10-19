#!/usr/bin/env bash
# Full fake-mail campaign: fresh local D1 with ALL contacts + one seed per mailbox, wrangler dev on :8789,
# then every send day simulated with the real sender (FakeTransport) and the Gmail-API sync (fake DSNs).
#   scripts/fake_full_run.sh setup | start | run FROM DAYS | verify | stop      (or: all)
set -euo pipefail
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}" no_proxy="127.0.0.1,localhost${no_proxy:+,$no_proxy}"
cd "$(dirname "$0")/.."
STATE=build/fake-state; PORT=${PORT:-8789}; BASE="http://127.0.0.1:$PORT"
[ -f .dev.vars ] || cp .dev.vars.example .dev.vars
case "${1:-all}" in
  setup)
    rm -rf "$STATE"
    npx wrangler d1 migrations apply ibls --local --persist-to "$STATE" >/dev/null
    python3 scripts/import_contacts.py --tracker "${TRACKER:-data/IBLS_outreach_tracker.xlsx}" --out build/import >/dev/null
    for f in build/import/*.sql; do npx wrangler d1 execute ibls --local --persist-to "$STATE" --file "$f" -y >/dev/null; done
    python3 scripts/seed_test.py --emails seed@example.com --per-mailbox --out build/seed_fake.sql >/dev/null
    npx wrangler d1 execute ibls --local --persist-to "$STATE" --file build/seed_fake.sql -y >/dev/null ;;
  start)
    setsid nohup npx wrangler dev --local --port "$PORT" --persist-to "$STATE" --test-scheduled > build/fake-dev.log 2>&1 < /dev/null &
    echo $! > build/fake-dev.pid
    for i in $(seq 1 60); do curl -sf -o /dev/null "$BASE/" && break; sleep 1; done
    python3 scripts/fake_full_run.py --base "$BASE" --init ;;
  run) python3 scripts/fake_full_run.py --base "$BASE" --from "$2" --days "$3" ${4:+--budget "$4"} ;;
  verify) python3 scripts/fake_full_run.py --base "$BASE" --verify ;;
  stop) kill -- -"$(cat build/fake-dev.pid)" 2>/dev/null || pkill -f "wrangler dev --local --port $PORT" || true ;;
  all) "$0" setup; "$0" start; trap '"$0" stop' EXIT; "$0" run 2026-10-26 70; "$0" verify ;;
esac
