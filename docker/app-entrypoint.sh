#!/usr/bin/env bash
set -euo pipefail

# st.secrets only reads .streamlit/secrets.toml -- there's no raw-env-var
# fallback -- so materialize it here from the env vars Compose injects
# (see .env / docker-compose.yml). Regenerated on every container start;
# never persisted outside the container filesystem.
mkdir -p /app/.streamlit
cat > /app/.streamlit/secrets.toml <<EOF
OPENAI_API_KEY = "${OPENAI_API_KEY:?OPENAI_API_KEY not set}"
AUTH_SERVICE_DOMAIN = "${AUTH_SERVICE_DOMAIN:?AUTH_SERVICE_DOMAIN not set}"
EOF

exec "$@"
