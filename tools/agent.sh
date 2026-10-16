#!/bin/bash
# A background agent, detached from the coordinator's session: a separate
# `claude -p` in its own git worktree (a fresh branch off origin/dev), so its
# commands never show up in the live conversation. It follows CLAUDE.md ›
# Background agents (land with tools/land.sh, keep the board, write its
# report to ~/Documents/Casper-reports/).
#
#   tools/agent.sh <slug> "<brief>"     or   tools/agent.sh <slug> @brief.md
#
# Prints only the agent's final reply (at most a few lines). Its full session
# is kept by Claude Code under ~/.claude/projects/; the reply also goes to
# ~/Documents/Casper-reports/logs/<when>-<slug>.log. The worktree is removed
# once its work is on origin/dev; otherwise it's kept, and the path printed.
# Start it in the background: it returns when the agent is done.
set -euo pipefail

# All in a function, called on the last line: bash reads the whole script
# before running any of it, so editing this file mid-run is safe.
main() {
  cd "$(dirname "$0")/.."
  local repo slug brief when log wt branch
  repo=$(pwd)
  slug=${1:?usage: tools/agent.sh <slug> "<brief>"}
  brief=${2:?usage: tools/agent.sh <slug> "<brief>"}
  [[ $brief == @* ]] && brief=$(cat "${brief#@}")
  when=$(date +%Y-%m-%d_%H%M)
  log="$HOME/Documents/Casper-reports/logs/$when-$slug.log"
  mkdir -p "$(dirname "$log")"

  git fetch -q origin
  wt="$HOME/.cache/casper-agents/$when-$slug"
  branch="agent/$when-$slug"
  mkdir -p "$(dirname "$wt")"
  git worktree add -q --no-track -b "$branch" "$wt" origin/dev  # --no-track: parallel launches would race on .git/config
  ln -s "$repo/.venv" "$wt/.venv"                      # tests, tools/vmgui.py
  cp "$repo"/*_server*.txt "$wt"/ 2>/dev/null || true  # untracked: app builds need them
  mkdir -p "$wt/agent/internal/config/embedded"        # untracked; go:embed needs the files
  touch "$wt"/agent/internal/config/embedded/{auth,relay,app}_server.txt

  (cd "$wt" && claude -p "$brief

Follow CLAUDE.md › Background agents. You are in your own worktree on branch $branch." \
    --dangerously-skip-permissions --output-format text < /dev/null > "$log" 2>&1) || echo "(the agent exited with an error)" >> "$log"
  # Agents don't reliably wait for the e2e runs they queue (land.sh starts
  # one; their session ends at their reply). Wait here, then add the runs'
  # summaries, so the one notice the coordinator gets carries the outcome.
  local started=$(date -r "$log" +%s) runs=()
  sleep 10
  while pgrep -f "tools/e2e.sh" >/dev/null; do sleep 60; done
  for r in "$HOME/Documents/Casper e2e runs"/*/; do
    [ "$(stat -f %B "$r")" -ge "$((started - 7200))" ] && [ -f "$r/notes.md" ] && [ "$(stat -f %m "$r/notes.md")" -ge "$started" ] && runs+=("$r")
  done
  cat "$log"
  for r in ${runs[@]+"${runs[@]}"}; do echo; echo "e2e: $r"; sed -n '1,8p' "$r/notes.md"; done

  if [ -z "$(git log --oneline "origin/dev..$branch" 2>/dev/null)" ] && git -C "$wt" diff --quiet && git -C "$wt" diff --cached --quiet; then
    git worktree remove --force "$wt" && git branch -q -D "$branch"
  else
    echo "(work not on dev yet; kept at $wt)"
  fi
}
main "$@"
