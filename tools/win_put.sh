#!/bin/bash
# Copies a local file to the Windows VM over SSH: tools/win_put.sh <local> <C:/remote/path>
set -euo pipefail
exec scp -i ~/.config/casper-vm/windows_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -P 2222 -q "$1" "noah@127.0.0.1:$2"
