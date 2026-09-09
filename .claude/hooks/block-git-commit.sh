#!/usr/bin/env bash
# PreToolUse hook (Bash tool): blocks any `git commit` invocation unless
# either (a) the exact command string carries an explicit ALLOW_GIT_COMMIT=1
# prefix, or (b) allow_git_commit.flag (alongside this script) contains "1"
# -- the assistant is only supposed to grant either when the user's
# *current* message explicitly asked for a commit (see the project memory
# "Commit/push/deploy only when asked"). The flag-file path exists because
# a Bash tool call's env vars don't persist to the next call in this
# harness, so a real env var can't be "set for the rest of the turn" --
# this is the stateful equivalent, and .git/hooks/post-commit resets it to
# "0" immediately after every commit, so one grant can't be silently reused
# for a second, unasked-for commit. This is a best-effort text-level guard
# against an *accidental* commit slipping through as an unconsidered step in
# some larger task, matching the exact failure mode that prompted the rule
# -- it is not a hardened parser or a security boundary against a
# deliberately adversarial command.
#
# Reads the PreToolUse JSON payload on stdin: {"tool_name": ..., "tool_input":
# {"command": ...}, ...}. Exits 0 (allow) unless this is an unflagged git
# commit, in which case it prints a reason to stderr and exits 2, which
# Claude Code treats as a blocking error and feeds back to the assistant.
set -euo pipefail

FLAG_FILE="$(dirname "$0")/allow_git_commit.flag"

payload="$(cat)"

tool_name=$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_name",""))' 2>/dev/null || echo "")
if [ "$tool_name" != "Bash" ]; then
    exit 0
fi

command=$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null || echo "")

# "git" then zero or more flag/short-arg tokens then "commit" as its own
# word -- tolerates `git -C path commit`, `git --no-pager commit`, plain
# `git commit`, chained after `&&`/`;`/`|`, etc. Not a real shell parser;
# a command containing the literal text "git commit" inside an unrelated
# string (e.g. an echo) would also match -- an acceptable false-positive
# given the alternative (a false negative letting a real commit through)
# is the actual risk this guards against.
if ! printf '%s' "$command" | grep -qE '(^|[;&|]|\()\s*git(\s+[A-Za-z0-9_./=-]+)*\s+commit(\s|$)'; then
    exit 0
fi

if printf '%s' "$command" | grep -q 'ALLOW_GIT_COMMIT=1'; then
    exit 0
fi

if [ -f "$FLAG_FILE" ] && [ "$(cat "$FLAG_FILE" 2>/dev/null)" = "1" ]; then
    exit 0
fi

echo "Blocked: 'git commit' requires either an explicit ALLOW_GIT_COMMIT=1 prefix in the exact same command, or allow_git_commit.flag set to 1 -- only do either when the user's current message explicitly asked for a commit. (See the 'Commit/push/deploy only when asked' project memory.) The flag auto-resets to 0 right after any commit (.git/hooks/post-commit), so it must be set again for each newly-authorized commit. Options: rerun as 'ALLOW_GIT_COMMIT=1 <the same command>', or 'echo 1 > $FLAG_FILE' first." >&2
exit 2
