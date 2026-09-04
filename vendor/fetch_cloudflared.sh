#!/usr/bin/env bash
# Downloads the cloudflared binaries bundled into the packaged installers.
# Not committed to git (they're ~20-55MB each) — run this before building.
set -euo pipefail
cd "$(dirname "$0")/cloudflared"

tmp="$(mktemp -d)"

curl -sL -o "$tmp/darwin-arm64.tgz" \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64.tgz
tar xzf "$tmp/darwin-arm64.tgz" -C "$tmp"
mv "$tmp/cloudflared" cloudflared-arm64
chmod +x cloudflared-arm64

curl -sL -o "$tmp/darwin-amd64.tgz" \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz
tar xzf "$tmp/darwin-amd64.tgz" -C "$tmp"
mv "$tmp/cloudflared" cloudflared-amd64
chmod +x cloudflared-amd64

curl -sL -o cloudflared-windows-amd64.exe \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe

rm -rf "$tmp"
echo "Fetched cloudflared binaries into $(pwd)"
