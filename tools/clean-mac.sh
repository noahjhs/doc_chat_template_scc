#!/bin/sh
# A clean macOS VM for walking through onboarding as a brand-new person.
# Uses Tart (https://tart.run), Apple's Virtualization.framework underneath.
#
#   tools/clean-mac.sh reset [agent]   start over from a saved starting point
#   tools/clean-mac.sh start           open the VM's window
#   tools/clean-mac.sh stop            shut it down
#
# Starting points (saved snapshots; local only, never pushed anywhere):
#   invitee-base        the invited person's Mac before Casper: Chrome
#                       (default browser), Gmail and Telegram signed in,
#                       realistic Documents; no sleep/lock, no update nags,
#                       remote login off. No AI agent -- the guide's path.
#   invitee-base-agent  the same, plus Claude Code installed and signed in
#                       -- the "paste this to your agent" path.
# Login: user admin; the password is in ~/.config/casper-vm/password.
# Without a saved snapshot, it falls back to a stock Sequoia image.
#
# Resets are cheap: each VM is a copy-on-write clone of its starting point.
set -e
VM=invitee-mac
FALLBACK=ghcr.io/cirruslabs/macos-sequoia-vanilla:latest

case "${1:-start}" in
start)
    tart list -q | grep -qx "$VM" || "$0" reset
    tart run "$VM" >/dev/null 2>&1 &
    echo "Starting $VM."
    ;;
reset)
    base=invitee-base
    [ "$2" = agent ] && base=invitee-base-agent
    tart list -q | grep -qx "$base" || { echo "No $base snapshot; using the stock image." >&2; base=$FALLBACK; }
    tart stop "$VM" 2>/dev/null || true
    tart delete "$VM" 2>/dev/null || true
    tart clone "$base" "$VM"
    tart set "$VM" --cpu 4 --memory 6144 --display 1440x900
    echo "$VM is fresh from $base. Run: $0 start"
    ;;
stop)
    tart stop "$VM"
    ;;
*)
    echo "usage: $0 reset [agent] | start | stop" >&2
    exit 2
    ;;
esac
