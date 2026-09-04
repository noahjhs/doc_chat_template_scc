#!/usr/bin/env bash
# Builds a standalone macOS executable of control_api.py, with cloudflared
# bundled inside so users don't need to install it separately.
# Run from the repo root: ./build/build_macos.sh
set -euo pipefail
cd "$(dirname "$0")/.."

ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then
    CLOUDFLARED_SRC="vendor/cloudflared/cloudflared-arm64"
else
    CLOUDFLARED_SRC="vendor/cloudflared/cloudflared-amd64"
fi

rm -rf build/pyinstaller_work
# Only remove this build's own prior outputs, and only the specific files
# within dist/DocChatControlAPI/ that we're about to regenerate — never the
# whole dist/ tree or that whole subdirectory, either of which can also hold
# runtime files (e.g. command_log.txt) from an already-running copy of the
# server launched from one of these paths.
rm -f dist/control_api dist/DocChatControlAPI-macos.zip
rm -f dist/DocChatControlAPI/control_api dist/DocChatControlAPI/README.md

STAGE_DIR="$(mktemp -d)"
CLOUDFLARED_STAGE="$STAGE_DIR/cloudflared"
cp "$CLOUDFLARED_SRC" "$CLOUDFLARED_STAGE"
chmod +x "$CLOUDFLARED_STAGE"

# Bake in the same LOCAL_AGENT_API_KEY the web app reads from its own
# secrets.toml, so a freshly downloaded build already matches it.
API_KEY_STAGE="$STAGE_DIR/baked_api_key.txt"
python3 -c "
import tomllib
with open('.streamlit/secrets.toml', 'rb') as f:
    key = tomllib.load(f)['LOCAL_AGENT_API_KEY']
print(key, end='')
" > "$API_KEY_STAGE"

# Optionally bake in the deployed web app's URL, so the server can open it
# in a new browser tab on launch. Skipped (not fatal) if app_url.txt is
# missing or empty.
ADD_APP_URL_ARGS=()
if [ -s app_url.txt ]; then
    APP_URL_STAGE="$STAGE_DIR/baked_app_url.txt"
    tr -d '\n' < app_url.txt > "$APP_URL_STAGE"
    ADD_APP_URL_ARGS=(--add-data "$APP_URL_STAGE:.")
else
    echo "app_url.txt is empty/missing — this build won't auto-open the web app."
fi

pyinstaller --onefile --name control_api \
    --add-binary "$CLOUDFLARED_STAGE:." \
    --add-data "$API_KEY_STAGE:." \
    ${ADD_APP_URL_ARGS[@]+"${ADD_APP_URL_ARGS[@]}"} \
    --workpath build/pyinstaller_work \
    --specpath build \
    control_api.py

mkdir -p dist/DocChatControlAPI
cp dist/control_api dist/DocChatControlAPI/
cp README_control_api.md dist/DocChatControlAPI/README.md 2>/dev/null || true
cd dist
zip -r DocChatControlAPI-macos.zip DocChatControlAPI
cd ..

echo "Built: dist/DocChatControlAPI-macos.zip"
