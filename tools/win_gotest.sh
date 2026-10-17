#!/bin/bash
# Runs Go package tests on the Windows VM (which has no Go): cross-compiles each
# package's test binary for windows/arm64 here, copies it over SSH and runs it
# there (with the bundled syncthing.exe, if put at C:\\casper-test, for the mirror tests). The VM must be up (tools/scenario.py start windows) and so holds the
# e2e lock.
#
#   tools/win_gotest.sh ./internal/secretstore ./internal/platform ...
#   tools/win_gotest.sh            # every package under agent/ that has tests
set -euo pipefail
cd "$(git rev-parse --show-toplevel)/agent"
KEY=~/.config/casper-vm/windows_ed25519
SSH=(-i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes)
pkgs=("$@")
[ ${#pkgs[@]} -gt 0 ] || while IFS= read -r p; do pkgs+=("$p"); done < <(go list -f '{{if or .TestGoFiles .XTestGoFiles}}{{.Dir}}{{end}}' ./... | sed "s#^$PWD#.#" | grep .)
out=$(mktemp -d)
ssh "${SSH[@]}" -p 2222 noah@127.0.0.1 'New-Item -ItemType Directory -Force C:\casper-test | Out-Null' >/dev/null
fail=0
for p in "${pkgs[@]}"; do
  name=$(echo "$p" | sed 's#^\./##; s#/#_#g')
  GOOS=windows GOARCH=arm64 CGO_ENABLED=0 go test -c -o "$out/$name.test.exe" "$p" 2>&1 | grep -v "no test files" || true
  [ -f "$out/$name.test.exe" ] || continue
  scp "${SSH[@]}" -P 2222 -q "$out/$name.test.exe" "noah@127.0.0.1:C:/casper-test/$name.test.exe"
  echo "== $p"
  # run from the package's own directory name so testdata-free tests behave; -test.v for visibility
  ssh "${SSH[@]}" -p 2222 noah@127.0.0.1 "cd C:\\casper-test; if (Test-Path C:\\casper-test\\syncthing.exe) { \$env:CASPER_TEST_SYNCTHING='C:\\casper-test\\syncthing.exe' }; .\\$name.test.exe '-test.count=1' '-test.v'" || fail=1
done
rm -rf "$out"
exit $fail
