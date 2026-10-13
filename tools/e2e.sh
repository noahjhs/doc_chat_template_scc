#!/bin/bash
# An unattended end-to-end run, in the background: deploys the commit to the
# test stack (tools/test_stack.sh), then a headless Claude Code follows the
# e2e-run skill (.claude/skills/e2e-run) in its own worktree at that commit,
# driving one VM over VNC (nothing on this Mac's screen) and a headless Riley
# on the mini. It reaches nobody: email is kept, and there's no Telegram.
#
#   tools/e2e.sh [scenario] [commit]   default: invite-headless, HEAD (must be pushed)
#
# The report lands in ~/Documents/Casper e2e runs/<when>/notes.md (screens/,
# transcripts and the runner's own log alongside). Prints the run directory
# and returns when the run is over; start it in the background.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)
SCENARIO=${1:-invite-headless}
REV=$(git rev-parse "${2:-HEAD}")
RUN="$HOME/Documents/Casper e2e runs/$(date +%Y-%m-%d_%H%M)"
mkdir -p "$RUN/screens"
echo "$RUN"

tools/test_stack.sh deploy "$REV" > "$RUN/deploy.log" 2>&1 || { echo "deploy failed: $RUN/deploy.log"; exit 1; }

# The commit under test, in a worktree of its own: the run's tools never
# change under it while work goes on in the main checkout.
WT="$HOME/.cache/casper-e2e/$(basename "$RUN")"
mkdir -p "$(dirname "$WT")"
git worktree add -q --detach "$WT" "$REV"
ln -s "$REPO/.venv" "$WT/.venv"   # vncdotool, for tools/vmgui.py
mkdir -p "$WT/agent/internal/config/embedded"   # untracked; go:embed needs the files to exist
touch "$WT"/agent/internal/config/embedded/{auth,relay,app}_server.txt
cleanup() { cd "$REPO" && git worktree remove --force "$WT" 2>/dev/null || true; }
trap cleanup EXIT

cd "$WT"
claude -p "Use the e2e-run skill. RUN=\"$RUN\" SCENARIO=$SCENARIO COMMIT=$REV. Work in this directory (a worktree of the commit under test)." \
  --dangerously-skip-permissions --output-format text > "$RUN/runner.log" 2>&1 || echo "runner exited with an error: $RUN/runner.log"
cd "$REPO"
tools/scenario.py stop "$SCENARIO" --env test > /dev/null 2>&1 || true   # in case the run didn't
echo "done: $RUN/notes.md"
