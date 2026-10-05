#!/bin/sh
# A clean macOS VM for walking through onboarding as a brand-new person:
# no Casper, no agent, no Homebrew -- a stock macOS Sequoia (Cirrus Labs'
# "vanilla" image: user admin / password admin). Uses Tart
# (https://tart.run), Apple's Virtualization.framework underneath.
#
#   tools/clean-mac.sh start   open the VM's window (creates it if needed)
#   tools/clean-mac.sh reset   throw it away and start over from fresh
#   tools/clean-mac.sh stop    shut it down
#
# Resets are cheap: the image is cached once (~25 GB) and each VM is a
# copy-on-write clone of it.
set -e
IMAGE=ghcr.io/cirruslabs/macos-sequoia-vanilla:latest
VM=invitee-mac

create() {
    tart clone "$IMAGE" "$VM"
    tart set "$VM" --cpu 4 --memory 6144 --display 1440x900
}

case "${1:-start}" in
start)
    tart list -q | grep -qx "$VM" || create
    tart run "$VM" >/dev/null 2>&1 &
    echo "Starting $VM (log in as admin / admin if asked)."
    ;;
reset)
    tart stop "$VM" 2>/dev/null || true
    tart delete "$VM" 2>/dev/null || true
    create
    echo "$VM is fresh. Run: $0 start"
    ;;
stop)
    tart stop "$VM"
    ;;
*)
    echo "usage: $0 start|reset|stop" >&2
    exit 2
    ;;
esac
