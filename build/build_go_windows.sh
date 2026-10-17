#!/usr/bin/env bash
# Builds Casper for Windows: a folder holding Casper.exe (agent/cmd/casper, no
# console window) and the bundled syncthing.exe (the mirroring engine, run by
# the daemon as a hidden child; see agent/internal/mirror). Cross-compiled from
# the Mac, no cgo. No installer, no signing and no download route yet: those
# are phase 6 of docs/product/windows.md.
#
#   ./build/build_go_windows.sh                 # DEPLOY_ENV=prod, arm64
#   DEPLOY_ENV=test GOARCH=amd64 ./build/build_go_windows.sh
#
# Output: build/go_windows_dist/Casper-<env>-<arch>/{Casper.exe,syncthing.exe}
# DEPLOY_ENV picks the *_server*.txt domain files to embed, as in
# build_go_macos.sh; set EMBED_DEV_DEFAULTS=1 to build with empty domain files
# (a bare build for the Windows VM's tests, which can't sign in anyway).
set -euo pipefail
cd "$(dirname "$0")/.."

GOARCH="${GOARCH:-arm64}"
case "$GOARCH" in arm64|amd64) ;; *) echo "GOARCH must be arm64 or amd64" >&2; exit 1 ;; esac
DEPLOY_ENV="${DEPLOY_ENV:-prod}"
case "$DEPLOY_ENV" in
    prod) SUFFIX="" ;;
    dev)  SUFFIX=".dev" ;;
    test) SUFFIX=".test" ;;
    *) echo "Unknown DEPLOY_ENV '$DEPLOY_ENV' -- expected 'prod', 'dev' or 'test'." >&2; exit 1 ;;
esac
AUTH_SERVER_FILE="auth_server${SUFFIX}.txt"
RELAY_SERVER_FILE="relay_server${SUFFIX}.txt"
APP_SERVER_FILE="app_server${SUFFIX}.txt"

mkdir -p agent/internal/config/embedded
if [ "${EMBED_DEV_DEFAULTS:-0}" = 1 ]; then
    : > agent/internal/config/embedded/auth_server.txt
    : > agent/internal/config/embedded/relay_server.txt
    : > agent/internal/config/embedded/app_server.txt
else
    [ -s "$AUTH_SERVER_FILE" ] || { echo "$AUTH_SERVER_FILE is missing/empty. Aborting." >&2; exit 1; }
    [ -s "$RELAY_SERVER_FILE" ] || { echo "$RELAY_SERVER_FILE is missing/empty. Aborting." >&2; exit 1; }
    cp "$AUTH_SERVER_FILE" agent/internal/config/embedded/auth_server.txt
    cp "$RELAY_SERVER_FILE" agent/internal/config/embedded/relay_server.txt
    if [ -s "$APP_SERVER_FILE" ]; then cp "$APP_SERVER_FILE" agent/internal/config/embedded/app_server.txt
    else : > agent/internal/config/embedded/app_server.txt; fi
fi
if [ -s assets/ghost.png ]; then cp assets/ghost.png agent/internal/dialog/assets/ghost.png
else : > agent/internal/dialog/assets/ghost.png; fi

OUT="build/go_windows_dist/Casper-${DEPLOY_ENV}-${GOARCH}"
rm -rf "$OUT"; mkdir -p "$OUT"
( cd agent && GOOS=windows GOARCH="$GOARCH" CGO_ENABLED=0 go build -trimpath -ldflags "-H=windowsgui -s -w" \
    -o "../$OUT/Casper.exe" ./cmd/casper )

# Syncthing: pinned, cached, and checked against the release's published SHA-256.
SYNCTHING_VERSION="v2.1.5"
ST_ASSET="syncthing-windows-${GOARCH}-${SYNCTHING_VERSION}.zip"
ST_CACHE="build/vendor/${ST_ASSET}"
mkdir -p build/vendor
if [ ! -f "$ST_CACHE" ]; then
    curl -fsSL "https://github.com/syncthing/syncthing/releases/download/${SYNCTHING_VERSION}/${ST_ASSET}" -o "$ST_CACHE.tmp"
    mv "$ST_CACHE.tmp" "$ST_CACHE"
fi
ST_SUMS="$(curl -fsSL "https://github.com/syncthing/syncthing/releases/download/${SYNCTHING_VERSION}/sha256sum.txt.asc")"
ST_EXPECTED="$(echo "$ST_SUMS" | awk -v f="$ST_ASSET" '$2 == f {print $1}')"
ST_ACTUAL="$(shasum -a 256 "$ST_CACHE" | awk '{print $1}')"
if [ -z "$ST_EXPECTED" ] || [ "$ST_EXPECTED" != "$ST_ACTUAL" ]; then
    echo "Syncthing checksum mismatch for $ST_ASSET (expected '$ST_EXPECTED', got '$ST_ACTUAL') -- refusing to bundle it." >&2
    exit 1
fi
ST_TMP="$(mktemp -d)"
unzip -q "$ST_CACHE" -d "$ST_TMP"
cp "$ST_TMP"/syncthing-windows-*/syncthing.exe "$OUT/syncthing.exe"
rm -rf "$ST_TMP"
echo "Built $OUT"
ls -l "$OUT"
