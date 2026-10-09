#!/bin/sh
# Fill each deployment's own URLs into the pages (and nginx.conf) at start.
set -e
VARS='${APP_URL} ${AUTH_URL} ${AGENTS_URL} ${DOWNLOAD_URL} ${WWW_URL} ${PAIR_SCHEME} ${AUTH_UPSTREAM}'
for f in /site/*.html /site/*.js; do envsubst "$VARS" < "$f" > "/usr/share/nginx/html/$(basename "$f")"; done
cp /site/*.css /site/*.svg /usr/share/nginx/html/
envsubst "$VARS" < /site/deploy/nginx.conf > /etc/nginx/conf.d/default.conf
