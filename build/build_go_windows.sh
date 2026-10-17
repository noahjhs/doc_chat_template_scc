#!/usr/bin/env bash
# Builds Casper for Windows: a folder holding Casper.exe (agent/cmd/casper, no
# console window) and the bundled syncthing.exe (the mirroring engine, run by
# the daemon as a hidden child; see agent/internal/mirror). Cross-compiled from
# the Mac, no cgo. Also makes what gets published (docs/product/windows.md §5):
# the installer, one .exe that carries the folder as a zip (agent/cmd/casper-setup,
# no admin prompt, installs to %LOCALAPPDATA%\Programs\Casper), and the bare zip.
#
#   ./build/build_go_windows.sh                 # DEPLOY_ENV=prod, arm64
#   DEPLOY_ENV=test GOARCH=amd64 ./build/build_go_windows.sh
#
# Output: build/go_windows_dist/Casper-<env>-<arch>/{Casper.exe,syncthing.exe}
#         dist/Casper-windows-<arch>-go.<env>.zip         (the folder)
#         dist/Casper-windows-<arch>-setup.<env>.exe      (the installer)
# (casper_service serves them as Casper-windows-<arch>.zip / -setup.exe from DIST_DIR.)
#
# Signing: Authenticode needs a certificate we don't have yet (the route is an
# open decision, windows.md §8). Set WIN_SIGN_CMD to a command that signs the
# file named by its last argument in place (e.g. "jsign --keystore ... --tsaurl
# http://timestamp.digicert.com", or osslsigncode); it then runs on Casper.exe,
# syncthing.exe's copy is left as the Syncthing project signed it, and on the
# installer last, so the installer carries the signed Casper.exe. Without it
# the build says "UNSIGNED" and carries on.
#   VERSION=0.1.0 sets the version stamped on the files (default 0.1.<commit count>).
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

# Version, icon and manifest as Windows resources (.syso, linked in by go build):
# asInvoker (never an admin prompt), long paths, per-monitor DPI, common controls v6.
case "$DEPLOY_ENV" in prod) PRODUCT="Casper" ;; dev) PRODUCT="Casper Dev" ;; test) PRODUCT="Casper Test" ;; esac
VERSION="${VERSION:-0.1.$(git rev-list --count HEAD 2>/dev/null || echo 0)}"
WINVER="${VERSION}.0"
WINRES="go run github.com/tc-hib/go-winres@v0.3.3"
make_resources() {   # <cmd dir name> <file description> <original filename>
    local dir="build/winres/$1"
    rm -rf "$dir"; mkdir -p "$dir"
    if [ -s assets/ghost.png ]; then cp assets/ghost.png "$dir/icon.png"; fi
    python3 - "$dir" "$2" "$3" "$PRODUCT" "$VERSION" "$WINVER" "$([ -s assets/ghost.png ] && echo 1 || echo 0)" <<'PY'
import json, sys
d, desc, orig, product, version, winver, icon = sys.argv[1:]
res = {}
if icon == "1":
    res["RT_GROUP_ICON"] = {"APP": {"0000": ["icon.png"]}}
res["RT_MANIFEST"] = {"#1": {"0409": {
    "identity": {"name": "Casper." + product.replace(" ", ""), "version": winver},
    "description": desc, "minimum-os": "win10", "execution-level": "as invoker",
    "ui-access": False, "auto-elevate": False, "dpi-awareness": "per monitor v2",
    "disable-theming": False, "disable-window-filtering": False,
    "high-resolution-scrolling-aware": False, "ultra-high-resolution-scrolling-aware": False,
    "long-path-aware": True, "printer-driver-isolation": False, "gdi-scaling": False,
    "segment-heap": False, "use-common-controls-v6": True}}}
res["RT_VERSION"] = {"#1": {"0000": {"fixed": {"file_version": winver, "product_version": winver},
    "info": {"0409": {"CompanyName": "Casper", "FileDescription": desc, "FileVersion": winver,
        "InternalName": orig, "OriginalFilename": orig, "ProductName": product, "ProductVersion": version}}}}}
json.dump(res, open(d + "/winres.json", "w"), indent=1)
PY
    rm -f "agent/cmd/$1"/rsrc_windows_*.syso
    ( cd agent && $WINRES make --in "../$dir/winres.json" --arch "$GOARCH" --out "cmd/$1/rsrc" >/dev/null )
}
cleanup_resources() { rm -f agent/cmd/casper/rsrc_windows_*.syso agent/cmd/casper-setup/rsrc_windows_*.syso agent/cmd/casper-setup/payload/payload.zip; }
trap cleanup_resources EXIT

sign() {
    if [ -n "${WIN_SIGN_CMD:-}" ]; then $WIN_SIGN_CMD "$1"; else echo "UNSIGNED: $1 (set WIN_SIGN_CMD to sign)"; fi
}

make_resources casper "$PRODUCT" Casper.exe
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

# Sign, then package: the zip, and the installer carrying that zip.
sign "$OUT/Casper.exe"
mkdir -p dist
ZIP="dist/Casper-windows-${GOARCH}-go.${DEPLOY_ENV}.zip"
rm -f "$ZIP"
( cd "$OUT" && zip -q -9 "../../../$ZIP" Casper.exe syncthing.exe )
cp "$ZIP" agent/cmd/casper-setup/payload/payload.zip
make_resources casper-setup "$PRODUCT Setup" Casper-Setup.exe
SETUP="dist/Casper-windows-${GOARCH}-setup.${DEPLOY_ENV}.exe"
( cd agent && GOOS=windows GOARCH="$GOARCH" CGO_ENABLED=0 go build -trimpath \
    -ldflags "-H=windowsgui -s -w -X casper-agent/internal/winpkg.Version=${VERSION}" \
    -o "../$SETUP" ./cmd/casper-setup )
sign "$SETUP"
echo "Built $OUT, $ZIP, $SETUP (version $VERSION)"
ls -l "$OUT" "$ZIP" "$SETUP"
