#!/bin/bash
# The mixed pair (docs/product/windows.md, phase 4): this Mac and the Windows VM
# mirror to each other through the real Manager and real Syncthing
# (agent/cmd/casper-mixedtest). The Mac listens on a port; the VM (which, as in
# production, listens only on loopback and dials out) reaches it at 10.0.2.2.
# Needs the VM up (tools/scenario.py start windows) and a Mac syncthing at
# $CASPER_TEST_SYNCTHING or ~/st-spike/bin/syncthing.
#
#   tools/win_mixedtest.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)/agent"
ST_MAC="${CASPER_TEST_SYNCTHING:-$HOME/st-spike/bin/syncthing}"
KEY=~/.config/casper-vm/windows_ed25519
SSH=(-i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes)
win() { ssh "${SSH[@]}" -p 2222 noah@127.0.0.1 "$1"; }
PORT=22101
T=$(mktemp -d); trap 'pkill -f "syncthing serve --home=$T" 2>/dev/null; rm -rf "$T"; [ -z "${MAC_PID:-}" ] || kill "$MAC_PID" 2>/dev/null || true; win "Get-Process syncthing,casper-mixedtest -ErrorAction SilentlyContinue | Stop-Process -Force" >/dev/null 2>&1 || true' EXIT

go build -o "$T/mixed" ./cmd/casper-mixedtest
GOOS=windows GOARCH=arm64 CGO_ENABLED=0 go build -o "$T/mixed.exe" ./cmd/casper-mixedtest
# a clean slate: nothing of an earlier run may be running or left behind
win 'New-Item -ItemType Directory -Force C:\casper-test | Out-Null; Get-Process syncthing,casper-mixedtest -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep 2; Remove-Item -Recurse -Force C:\casper-test\mixed -ErrorAction SilentlyContinue; if (Test-Path C:\casper-test\mixed) { cmd /c rmdir /s /q C:\casper-test\mixed }; exit 0' >/dev/null
[ "$(win 'Test-Path C:\casper-test\mixed' | tr -d '\r')" = False ] || { echo "couldn't clear C:/casper-test/mixed" >&2; exit 1; }
scp "${SSH[@]}" -P 2222 -q "$T/mixed.exe" noah@127.0.0.1:C:/casper-test/casper-mixedtest.exe
[ "$(win 'Test-Path C:\casper-test\syncthing.exe' | tr -d '\r')" = True ] || { echo "put syncthing.exe at C:/casper-test first (tools/win_put.sh)" >&2; exit 1; }

MAC_ID=$("$T/mixed" -role mac -syncthing "$ST_MAC" -dir "$T/mac" -listen "tcp://0.0.0.0:$PORT" id 2>/dev/null | awk '/^DEVICE/{print $2}')
WIN_ID=$(win 'C:\casper-test\casper-mixedtest.exe -role win -syncthing C:\casper-test\syncthing.exe -dir C:\casper-test\mixed id 2>$null' | tr -d '\r' | awk '/^DEVICE/{print $2}')
echo "mac $MAC_ID"; echo "win $WIN_ID"
[ -n "$MAC_ID" ] && [ -n "$WIN_ID" ]

"$T/mixed" -role mac -syncthing "$ST_MAC" -dir "$T/mac" -listen "tcp://0.0.0.0:$PORT" -peer-id "$WIN_ID" run > "$T/mac.out" 2> "$T/mac.err" &
MAC_PID=$!
rc=0
win "C:\\casper-test\\casper-mixedtest.exe -role win -syncthing C:\\casper-test\\syncthing.exe -dir C:\\casper-test\\mixed -peer-id $MAC_ID -peer-addr tcp://10.0.2.2:$PORT run 2>\$null" | tr -d '\r' > "$T/win.out" || rc=1
wait $MAC_PID || rc=1
echo "--- windows"; tail -3 "$T/win.out"; echo "--- mac"; tail -3 "$T/mac.out"
grep -q '"result":"ok"' "$T/win.out" && grep -q '"result":"ok"' "$T/mac.out" || rc=1
[ $rc = 0 ] && echo "MIXED PAIR OK" || { echo "MIXED PAIR FAILED"; tail -20 "$T/mac.err"; win 'Get-Content C:\casper-test\mixed\st\syncthing.log -Tail 15'; }
exit $rc
