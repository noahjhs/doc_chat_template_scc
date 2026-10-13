#!/bin/bash
# Lands a background agent's work: run it from the agent's own git worktree,
# on its branch, with everything committed.
#
#   tools/land.sh
#
# 1. Rebases the branch onto origin/dev. On a conflict it stops (the rebase
#    left in progress) and lists the conflicting files: resolve them and
#    `git rebase --continue`, then run this again; or `git rebase --abort`
#    and escalate.
# 2. Tests: pytest (without the two integration suites), and `go test ./...`
#    in agent/ when agent/ changed. Stops on a failure.
# 3. Under a lock (~/.cache/casper-land.lock: one landing/deploy at a time),
#    pushes the commits to origin/dev, fast-forward only (if dev moved
#    meanwhile: rebase and retest first), and deploys dev on the mini: auth
#    and www, plus the dev Mac app when agent/ or build/ changed (served from
#    dev's dist/ and reinstalled at the mini's /Applications/Casper.app).
# 4. Releases the lock and starts tools/e2e.sh for the landed commit in the
#    background (e2e runs queue on their own lock), from the main checkout.
#    Prints where the report will be.
set -euo pipefail

MINI=casper-mini
PYTEST=(.venv/bin/python -m pytest tests/ -q --ignore=tests/test_backup_integration.py --ignore=tests/test_mirror_integration.py)

say() { echo "land: $*"; }
die() { echo "land: $*" >&2; exit 1; }
on_mini() { ssh "$MINI" "source ~/.orbstack/shell/init.zsh; set -e; $1"; }

# Rebases onto origin/dev; on a conflict, stops with the files in question.
rebase() {
  git fetch -q origin
  git merge-base --is-ancestor origin/dev HEAD && return 0
  if ! git rebase -q origin/dev >/dev/null 2>&1; then
    echo "land: rebase onto origin/dev stopped on conflicts in:" >&2
    git diff --name-only --diff-filter=U | sed 's/^/  /' >&2
    echo "land: resolve them, git add, git rebase --continue, then run tools/land.sh again (or git rebase --abort and escalate)" >&2
    exit 1
  fi
  say "rebased onto origin/dev ($(git rev-parse --short origin/dev))"
}

# Whether this landing (origin/dev..HEAD) touches any of the given paths.
changed() { [ -n "$(git diff --name-only origin/dev HEAD -- "$@")" ]; }

run_tests() {
  say "pytest"
  "${PYTEST[@]}" > "$LOG/pytest.log" 2>&1 || { tail -30 "$LOG/pytest.log" >&2; die "tests failed (full log: $LOG/pytest.log)"; }
  tail -1 "$LOG/pytest.log"
  if changed agent; then
    say "go test ./... (agent/ changed)"
    (cd agent && go test ./... > "$LOG/gotest.log" 2>&1) || { grep -v '^ok' "$LOG/gotest.log" | tail -40 >&2; die "go tests failed (full log: $LOG/gotest.log)"; }
  fi
}

deploy_app() {
  say "building the dev Mac app (agent/ or build/ changed)"
  cp "$MAIN"/{auth,relay,app}_server.dev.txt .   # untracked, per deployment
  DEPLOY_ENV=dev ./build/build_go_macos.sh > "$LOG/build.log" 2>&1 || die "app build failed: $LOG/build.log"
  scp -q dist/Casper-macos-go.dev.zip "$MINI:doc_chat_template_scc/dist/Casper-macos.zip"
  on_mini '
    t=$(mktemp -d); cd $t && unzip -q ~/doc_chat_template_scc/dist/Casper-macos.zip
    pkill -f "^/Applications/Casper.app/Contents/MacOS/Casper" || true; sleep 2
    rm -rf /Applications/Casper.app && mv Casper.app /Applications/ && rm -rf $t
    open /Applications/Casper.app'
  say "dev app served and reinstalled on the mini"
}

deploy() {
  say "deploying dev on the mini (auth, www)"
  on_mini 'cd ~/doc_chat_template_scc && git pull -q && docker compose up -d --build auth www 2>&1 | grep -v -E "^ *(#|=>)" | tail -4'
  if [ "$APP" = 1 ]; then deploy_app; fi
}

main() {
  cd "$(git rev-parse --show-toplevel)"
  MAIN=$(cd "$(git rev-parse --git-common-dir)/.." && pwd)   # the main checkout
  [ -e .venv ] || ln -s "$MAIN/.venv" .venv
  mkdir -p agent/internal/config/embedded   # untracked; go:embed needs the files to exist
  touch agent/internal/config/embedded/{auth,relay,app}_server.txt
  local branch; branch=$(git symbolic-ref --short -q HEAD) || die "not on a branch"
  [ -z "$(git status --porcelain --untracked-files=no)" ] || die "uncommitted changes: commit them first"
  LOG=$(mktemp -d "${TMPDIR:-/tmp}/casper-land.XXXX")

  rebase
  [ -n "$(git rev-list origin/dev..HEAD)" ] || { say "nothing to land: $branch is already in origin/dev"; exit 0; }
  run_tests

  source tools/lock.sh
  trap 'lock_release land' EXIT
  lock_take land
  # dev may have moved while we tested or waited: rebase and retest until it holds still.
  while git fetch -q origin && ! git merge-base --is-ancestor origin/dev HEAD; do
    say "origin/dev moved; rebasing and retesting"
    rebase
    run_tests
  done
  APP=0; if changed agent build; then APP=1; fi   # before the push: origin/dev..HEAD is this landing
  local n; n=$(git rev-list --count origin/dev..HEAD)
  git push -q origin HEAD:dev || die "push to origin/dev refused"
  REV=$(git rev-parse HEAD)
  say "pushed $n commit(s) to origin/dev, now ${REV:0:9}"
  deploy
  lock_release land

  # e2e from the main checkout, so it outlives this worktree.
  local elog="$HOME/.cache/casper-e2e/land-${REV:0:9}.log"
  mkdir -p "$(dirname "$elog")"
  nohup "$MAIN/tools/e2e.sh" invite-headless "$REV" > "$elog" 2>&1 < /dev/null &
  sleep 3
  local run; run=$(grep -m1 'e2e runs/' "$elog" || true)
  if [ -n "$run" ]; then
    say "landed ${REV:0:9}; e2e running, report: $run/notes.md (log: $elog)"
  else
    say "landed ${REV:0:9}; e2e queued behind another run (lock: ~/.cache/casper-e2e.lock), its report path will be the first line of $elog"
  fi
}
main "$@"
