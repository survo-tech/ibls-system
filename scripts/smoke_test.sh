#!/usr/bin/env bash
# Post-deploy smoke test. Read-only: it never sends mail or reserves contacts.
#   BASE=https://survo.tech API_TOKEN=... [DASH_USER=admin DASH_PASS=...] [RID=TEST0001] scripts/smoke_test.sh
set -uo pipefail
BASE=${BASE:-https://survo.tech}
RID=${RID:-TEST0001}
fail=0
check() { # name expected actual
  if [ "$2" = "$3" ]; then printf "  ok    %-48s %s\n" "$1" "$3"; else printf "  FAIL  %-48s expected %s got %s\n" "$1" "$2" "$3"; fail=1; fi
}
code() { curl -s -o /dev/null -w "%{http_code}" "$@"; }

echo "Smoke test against $BASE"
check "GET / (static site)"            200 "$(code "$BASE/")"
check "GET /hero.webp"                 200 "$(code "$BASE/hero.webp")"
check "GET /site.css"                  200 "$(code "$BASE/site.css")"
check "GET /bimi/logo.svg content type" "image/svg+xml" "$(curl -s -o /dev/null -w "%{content_type}" "$BASE/bimi/logo.svg")"
case "$BASE" in https://*) check "www redirects to apex" 301 "$(code "https://www.${BASE#https://}/")";; esac
loc=$(curl -s -o /dev/null -w "%{redirect_url}" -A "smoke-test-bot" "$BASE/s/$RID")
check "GET /s/$RID -> Kobo with rid"   "https://ee.kobotoolbox.org/jbh8Urj6?d[rid]=$RID" "$loc"
check "GET /optout/$RID (confirm page)" 200 "$(code "$BASE/optout/$RID")"
check "GET /api/settings w/o token"    401 "$(code "$BASE/api/settings")"
dash_noauth=$(code "$BASE/dashboard")
if [ "$dash_noauth" = "302" ] || [ "$dash_noauth" = "401" ] || [ "$dash_noauth" = "403" ]; then check "GET /dashboard w/o auth is blocked" blocked blocked; else check "GET /dashboard w/o auth is blocked" "302|401|403" "$dash_noauth"; fi
if [ -n "${API_TOKEN:-}" ]; then
  H="Authorization: Bearer $API_TOKEN"
  check "GET /api/settings"            200 "$(code -H "$H" "$BASE/api/settings")"
  # max=0 never reserves anything
  check "GET /api/next-batch?max=0"    200 "$(code -H "$H" "$BASE/api/next-batch?mailbox=participate%40survo.tech&max=0&run_id=smoke")"
  check "GET /api/summary"             200 "$(code -H "$H" "$BASE/api/summary")"
  echo "  settings: $(curl -s -H "$H" "$BASE/api/settings" | python3 -c 'import json,sys; s=json.load(sys.stdin)["settings"]; print({k: s[k] for k in ("test_mode","paused","start_date","test_recipients")})')"
fi
if [ -n "${DASH_USER:-}" ]; then
  check "GET /dashboard (basic auth)"  200 "$(code -u "$DASH_USER:$DASH_PASS" "$BASE/dashboard")"
  check "GET /dashboard/api/summary"   200 "$(code -u "$DASH_USER:$DASH_PASS" "$BASE/dashboard/api/summary")"
fi
[ $fail = 0 ] && echo "All checks passed." || { echo "Some checks failed."; exit 1; }
