#!/usr/bin/env bash
# Builds a standalone macOS executable of casper_tool.py (Casper), with
# cloudflared bundled inside so users don't need to install it separately.
# Run from the repo root: ./build/build_macos.sh
set -euo pipefail
cd "$(dirname "$0")/.."

ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then
    CLOUDFLARED_SRC="vendor/cloudflared/cloudflared-arm64"
else
    CLOUDFLARED_SRC="vendor/cloudflared/cloudflared-amd64"
fi

rm -rf build/pyinstaller_work build/pyinstaller_dist
# Only remove this build's own prior outputs, and only the specific files
# within dist/Casper/ that we're about to regenerate — never the whole
# dist/ tree or that whole subdirectory, either of which can also hold
# runtime files (e.g. command_log.txt) from an already-running copy of the
# server launched from one of these paths.
rm -f dist/Casper-macos.zip
rm -f dist/Casper/Casper dist/Casper/README.md

STAGE_DIR="$(mktemp -d)"
CLOUDFLARED_STAGE="$STAGE_DIR/cloudflared"
cp "$CLOUDFLARED_SRC" "$CLOUDFLARED_STAGE"
chmod +x "$CLOUDFLARED_STAGE"

# Bake in the deployed web app's domain (just the host[:port], e.g.
# my-app.streamlit.app — no scheme, no path; casper_tool.py appends
# https:// and /chat), so the server can open it in a new browser tab on
# launch. Skipped (not fatal) if app_server.txt is missing or empty.
ADD_APP_SERVER_ARGS=()
if [ -s app_server.txt ]; then
    APP_SERVER_STAGE="$STAGE_DIR/baked_app_server.txt"
    tr -d '\n' < app_server.txt > "$APP_SERVER_STAGE"
    ADD_APP_SERVER_ARGS=(--add-data "$APP_SERVER_STAGE:.")
else
    echo "app_server.txt is empty/missing — this build won't auto-open the web app."
fi

# Bake in the auth service's domain. Unlike app_server.txt, this one is NOT
# optional -- casper_tool.py can't sign in without it, so catch a missing
# value here at build time instead of at every downloader's first run.
if [ ! -s auth_server.txt ]; then
    echo "auth_server.txt is missing/empty -- the built binary couldn't sign in. Aborting." >&2
    exit 1
fi
AUTH_SERVER_STAGE="$STAGE_DIR/baked_auth_server.txt"
tr -d '\n' < auth_server.txt > "$AUTH_SERVER_STAGE"

# Bundle the ghost icon shown in the native sign-out farewell dialog
# (show_farewell_dialog() in casper_tool.py). Not fatal if missing -- that
# dialog just falls back to no custom icon.
ADD_GHOST_ICON_ARGS=()
if [ -s assets/ghost.png ]; then
    GHOST_ICON_STAGE="$STAGE_DIR/ghost.png"
    cp assets/ghost.png "$GHOST_ICON_STAGE"
    ADD_GHOST_ICON_ARGS=(--add-data "$GHOST_ICON_STAGE:.")
else
    echo "assets/ghost.png is missing -- the farewell dialog won't have a custom icon."
fi

# --distpath keeps PyInstaller's raw single-file output (dist/Casper, a
# file) out of dist/Casper/ (the staging directory below, containing that
# same binary alongside the README) -- those two would otherwise collide
# on the same path.
pyinstaller --onefile --name Casper \
    --add-binary "$CLOUDFLARED_STAGE:." \
    --add-data "$AUTH_SERVER_STAGE:." \
    ${ADD_APP_SERVER_ARGS[@]+"${ADD_APP_SERVER_ARGS[@]}"} \
    ${ADD_GHOST_ICON_ARGS[@]+"${ADD_GHOST_ICON_ARGS[@]}"} \
    --distpath build/pyinstaller_dist \
    --workpath build/pyinstaller_work \
    --specpath build \
    casper_tool.py

mkdir -p dist/Casper
cp build/pyinstaller_dist/Casper dist/Casper/
cp README_casper.md dist/Casper/README.md 2>/dev/null || true
# Runtime state from a previous local test run under this same directory --
# never part of the build, and must never end up inside the distributable.
rm -f dist/Casper/session.json dist/Casper/command_log.txt
cd dist
zip -r Casper-macos.zip Casper
cd ..

echo "Built: dist/Casper-macos.zip"
