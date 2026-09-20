#!/usr/bin/env bash
# Post-deploy smoke check -- formalizes the ad hoc curl checks run by hand
# after every deploy this session. Hits every remaining Streamlit page (200
# expected -- Streamlit itself always returns the app shell, so this only
# catches the container being down/crash-looping, not a bad page) and a
# couple of casper_service endpoints with a bogus token (401 expected --
# confirms the service is up AND its auth gate is actually enforcing,
# rather than e.g. silently misconfigured to accept anything).
#
# Usage: scripts/smoke_test.sh [dev|prod]
# Defaults to dev. Override the domains directly instead via
# APP_DOMAIN/AUTH_DOMAIN env vars (e.g. for a local `streamlit run` +
# `uvicorn` pair on localhost).
set -uo pipefail

ENVIRONMENT="${1:-dev}"
if [[ "$ENVIRONMENT" == "dev" ]]; then
  APP_DOMAIN="${APP_DOMAIN:-dev-app.casperagent.dev}"
  AUTH_DOMAIN="${AUTH_DOMAIN:-dev-auth.casperagent.dev}"
elif [[ "$ENVIRONMENT" == "prod" ]]; then
  APP_DOMAIN="${APP_DOMAIN:-app.casperagent.dev}"
  AUTH_DOMAIN="${AUTH_DOMAIN:-auth.casperagent.dev}"
else
  echo "Usage: $0 [dev|prod]  (or set APP_DOMAIN/AUTH_DOMAIN directly)" >&2
  exit 2
fi

CURL=/usr/bin/curl
FAILURES=0

check() {
  local description="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    echo "  OK   $description ($actual)"
  else
    echo "  FAIL $description (expected $expected, got $actual)"
    FAILURES=$((FAILURES + 1))
  fi
}

echo "App ($APP_DOMAIN):"
# Only the public, unauthenticated pages -- sign-in/sign-up and everything
# gated behind them (Environments, Settings) moved to the harness; those
# pages no longer exist in this app at all.
for path in "" "download"; do
  code=$("$CURL" -s -o /dev/null -w "%{http_code}" --max-time 10 "https://${APP_DOMAIN}/${path}")
  check "/$path" "200" "$code"
done

echo "Auth service ($AUTH_DOMAIN), bogus-token requests (expect 401 -- confirms the service is up and actually gating):"
code=$("$CURL" -s -o /dev/null -w "%{http_code}" --max-time 10 -X POST "https://${AUTH_DOMAIN}/policies/eval" \
  -H "Authorization: Bearer smoke-test-bogus-token" -H "Content-Type: application/json" \
  -d '{"policy_layer_ids": [], "positional_args": []}')
check "POST /policies/eval" "401" "$code"

code=$("$CURL" -s -o /dev/null -w "%{http_code}" --max-time 10 -X POST "https://${AUTH_DOMAIN}/conversations/step" \
  -H "Authorization: Bearer smoke-test-bogus-token" -H "Content-Type: application/json" \
  -d '{"message": "hi"}')
check "POST /conversations/step" "401" "$code"

code=$("$CURL" -s -o /dev/null -w "%{http_code}" --max-time 10 "https://${AUTH_DOMAIN}/hosts" \
  -H "Authorization: Bearer smoke-test-bogus-token")
check "GET /hosts" "401" "$code"

echo
echo "Functional check (signup -> pair -> policy -> mock tool call, see functional_smoke_test.py):"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if python3 -c "import harness.client" >/dev/null 2>&1; then
  if AUTH_DOMAIN="$AUTH_DOMAIN" python3 "$SCRIPT_DIR/functional_smoke_test.py" "$ENVIRONMENT"; then
    :
  else
    FAILURES=$((FAILURES + 1))
  fi
else
  echo "  SKIP (harness isn't installed in this environment -- pip install -e harness/ to include this check)"
fi

echo
if [[ "$FAILURES" -eq 0 ]]; then
  echo "All checks passed."
  exit 0
else
  echo "$FAILURES check(s) failed."
  exit 1
fi
