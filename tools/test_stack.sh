#!/bin/bash
# The end-to-end test deployment: a third Casper stack on the mini, next to dev
# and prod, so automated test runs never collide with dev deploys. It has its
# own checkout (pinned to the commit under test), containers (casper-*-test),
# database, catcher, web addresses (test-www/test-app/test-auth/test-relay
# .casperagent.dev) and app build (DEPLOY_ENV=test: "Casper Test", its own
# config directory and casper-test:// scheme). It shares dev's Syncthing relay.
# Email is kept, not sent (MAIL_SINK; read it with GET /dev/mail), and there's
# no Telegram, so a run reaches nobody.
#
#   tools/test_stack.sh setup            once: checkout, .env.test, DNS, tunnel routes
#   tools/test_stack.sh deploy [commit]  run <commit> (default HEAD) there; rebuilds the app only if agent/ changed
#   tools/test_stack.sh status           what's running, and at which commit
#   tools/test_stack.sh down             stop it (data is kept)
set -euo pipefail
cd "$(dirname "$0")/.."

MINI=casper-mini
DIR=doc_chat_template_scc-test   # on the mini, in ~
TUNNEL=cfc0fe09-caf3-463b-b015-37d4639c7e1e
HOSTS="test-www test-app test-auth test-relay"
COMPOSE="docker compose -p casper-test --env-file .env.test"
on_mini() { ssh "$MINI" "source ~/.orbstack/shell/init.zsh; set -e; $1"; }

setup() {
  on_mini "
    [ -d ~/$DIR ] || git clone -q \$(git -C ~/doc_chat_template_scc remote get-url origin) ~/$DIR
    cd ~/$DIR && mkdir -p dist
    if [ ! -f .env.test ]; then
      # dev's settings, minus Telegram and SMTP, pointed at the test addresses
      grep -vE '^(TELEGRAM_|SMTP_|MAIL_FROM|CATCHER_)' ~/doc_chat_template_scc/.env \
        | sed -e 's/dev-auth\./test-auth./g' -e 's/dev-app\./test-app./g' -e 's/dev-www\./test-www./g' > .env.test
      ADDRS=\$(grep '^CATCHER_ADDRESSES=' ~/doc_chat_template_scc/.env | cut -d= -f2- | sed 's/:22100/:22101/g')
      cat >> .env.test <<EOF

# The end-to-end test stack (tools/test_stack.sh)
ENV_SUFFIX=-test
AUTH_ENV_FILE=.env.test
WWW_DEBUG_PORT=8081
AUTH_DEBUG_PORT=8101
RELAY_DEBUG_PORT=8601
CATCHER_PORT=22101
PUBLIC_APP_URL=https://test-app.casperagent.dev
PUBLIC_AUTH_URL=https://test-auth.casperagent.dev
PUBLIC_AGENTS_URL=test-auth.casperagent.dev/agents.md
PUBLIC_DOWNLOAD_URL=https://test-auth.casperagent.dev/download/casper/macos
PAIR_SCHEME=casper-test
MAIL_SINK=/data/mail
CATCHER_URL=http://casper-catcher-test:8384
CATCHER_API_KEY=\$(openssl rand -hex 16)
CATCHER_ADDRESSES=\$ADDRS
EOF
      chmod 600 .env.test
    fi
    cfg=~/casper-infra/cloudflared/config.yml
    if ! grep -q test-auth \$cfg; then
      cp \$cfg \$cfg.bak-\$(date +%Y%m%d%H%M)
      python3 - \$cfg <<'PY'
import sys
p = sys.argv[1]; s = open(p).read()
add = ''.join(f'  - hostname: test-{h}.casperagent.dev\n    service: http://casper-{svc}-test:{port}\n'
              for h, svc, port in [('www', 'www', 8080), ('app', 'www', 8080), ('auth', 'auth', 8100), ('relay', 'relay', 8600)])
s = s.replace('  - service: http_status:404', add + '  - service: http_status:404')
open(p, 'w').write(s)
PY
      for h in $HOSTS; do
        docker run --rm -u 0 -v ~/casper-infra/cloudflared:/etc/cloudflared cloudflare/cloudflared:latest \
          tunnel --origincert /etc/cloudflared/cert.pem route dns $TUNNEL \$h.casperagent.dev || true
      done
      docker restart casper-cloudflared >/dev/null
    fi
    echo 'test stack set up'"
}

deploy() {
  local rev; rev=$(git rev-parse "${1:-HEAD}")
  git fetch -q origin
  [ -n "$(git branch -r --contains "$rev")" ] || { echo "${rev:0:9} isn't on GitHub yet (the mini fetches it from there): push it first"; exit 1; }
  # The app: rebuilt only when agent/ (or the build) changed since the last test build.
  local tree; tree=$(git rev-parse "$rev:agent")-$(git rev-parse "$rev:build")
  if [ "$(cat dist/Casper-macos-go.test.tree 2>/dev/null)" != "$tree" ]; then
    local wt; wt=$(mktemp -d)/build
    git worktree add -q --detach "$wt" "$rev"
    cp auth_server.test.txt relay_server.test.txt app_server.test.txt "$wt/"
    mkdir -p "$wt/agent/internal/config/embedded"   # untracked: the build stages the domains here
    (cd "$wt" && DEPLOY_ENV=test ./build/build_go_macos.sh > /tmp/casper_test_build.log 2>&1) \
      || { echo "app build failed: /tmp/casper_test_build.log"; git worktree remove --force "$wt"; exit 1; }
    cp "$wt/dist/Casper-macos-go.test.zip" dist/
    git worktree remove --force "$wt"
    echo "$tree" > dist/Casper-macos-go.test.tree
  fi
  scp -q dist/Casper-macos-go.test.zip "$MINI:$DIR/dist/Casper-macos.zip"
  on_mini "cd ~/$DIR && git fetch -q origin && git checkout -q --detach $rev && $COMPOSE up -d --build www auth catcher relay 2>&1 | grep -v -E '^ *(#|=>)' | tail -4"
  for _ in $(seq 30); do
    curl -fs https://test-auth.casperagent.dev/invites/info?code=x >/dev/null 2>&1 && { echo "test stack at ${rev:0:9}"; return; }
    sleep 2
  done
  echo "test stack deployed at ${rev:0:9}, but test-auth isn't answering yet"
}

case "${1:-}" in
  setup) setup ;;
  deploy) deploy "${2:-}" ;;
  status) on_mini "cd ~/$DIR && echo commit \$(git rev-parse --short HEAD) && docker ps --filter name=-test --format '{{.Names}}\t{{.Status}}'" ;;
  down) on_mini "cd ~/$DIR && $COMPOSE stop www auth catcher relay" ;;
  *) sed -n 2,15p "$0"; exit 2 ;;
esac
