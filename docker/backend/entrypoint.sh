#!/bin/sh
# Stamp this backend's identity into every served HTML document, mirroring the
# VM path's stamp_backend_identity. Runs once at container start, then hands off
# to Apache. Idempotent: files already carrying the marker are skipped.
set -eu

: "${BACKEND_HOST:?BACKEND_HOST must be set}"
docroot=/usr/local/apache2/htdocs
meta="<meta name=\"x-backend-host\" content=\"${BACKEND_HOST}\">"

for html in $(grep -rlI --include='*.html' -e '<head' "$docroot" 2>/dev/null || true); do
    grep -q 'x-backend-host' "$html" && continue
    sed -i "0,/<[Hh][Ee][Aa][Dd][^>]*>/s||&\\n    ${meta}|" "$html"
done

exec httpd-foreground
