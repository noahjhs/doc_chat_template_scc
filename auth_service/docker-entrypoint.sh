#!/usr/bin/env bash
set -euo pipefail

# db.py's sqlite3.connect(DB_PATH) does not create DB_PATH's parent
# directory itself -- pointing AUTH_DB_PATH at a fresh volume mount would
# otherwise fail on first run with "unable to open database file".
mkdir -p "$(dirname "${AUTH_DB_PATH:-/data/users.db}")"

exec "$@"
