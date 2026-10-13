#!/bin/bash
# One-at-a-time locks for tools/land.sh and tools/e2e.sh, shared by every
# worktree on this Mac. macOS has no flock, so a lock is a directory
# (mkdir is atomic) holding the holder's pid; a lock whose holder is gone is
# stale and gets taken over.
#
#   source tools/lock.sh
#   lock_take land      waits until ~/.cache/casper-land.lock is ours
#   lock_release land   lets the next one in (only if we hold it)
#   lock_holder land    the holder's pid, if any

lock_dir() { echo "$HOME/.cache/casper-$1.lock"; }

lock_holder() { cat "$(lock_dir "$1")/pid" 2>/dev/null || true; }

lock_take() {
  local dir pid said=""
  dir=$(lock_dir "$1")
  mkdir -p "$(dirname "$dir")"
  until mkdir "$dir" 2>/dev/null; do
    pid=$(lock_holder "$1")
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
      echo "$1 lock: holder $pid is gone, taking it over" >&2
      rm -rf "$dir"
      continue
    fi
    # No pid: the holder is between mkdir and writing it, or died right there.
    if [ -z "$pid" ] && [ $(( $(date +%s) - $(stat -f %m "$dir" 2>/dev/null || date +%s) )) -gt 60 ]; then
      rm -rf "$dir"
      continue
    fi
    [ -n "$said" ] || { echo "$1 lock: waiting for ${pid:-another run} ($dir)" >&2; said=1; }
    sleep 5
  done
  echo $$ > "$dir/pid"
}

lock_release() {
  local dir; dir=$(lock_dir "$1")
  [ "$(lock_holder "$1")" = "$$" ] && rm -rf "$dir"
  return 0
}
