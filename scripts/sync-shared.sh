#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
SHARED="$REPO_ROOT/shared"

# Target directories
MANSIO="$REPO_ROOT/mansio/src/mansio"
CLIENT="$REPO_ROOT/mansio-client/src/mansio_client"

# File mapping: shared_path -> (mansio_dest, client_dest)
# transport.py has different filenames in each package
sync_file() {
    local src="$1" mansio_dest="$2" client_dest="$3"

    # Copy to mansio, replace __PKG__ with mansio
    sed 's/__PKG__/mansio/g' "$src" > "$mansio_dest"

    # Copy to client, replace __PKG__ with mansio_client
    sed 's/__PKG__/mansio_client/g' "$src" > "$client_dest"
}

# Types (no placeholder needed, identical)
cp "$SHARED/types.py" "$MANSIO/types.py"
cp "$SHARED/types.py" "$CLIENT/types.py"

# Transport (different filenames)
sync_file "$SHARED/transport.py" "$MANSIO/transport_http.py" "$CLIENT/transport.py"

# Vendor files
cp "$SHARED/_vendor/httpclient.py" "$MANSIO/_vendor/httpclient.py"
cp "$SHARED/_vendor/httpclient.py" "$CLIENT/_vendor/httpclient.py"

sync_file "$SHARED/_vendor/sse.py" "$MANSIO/_vendor/sse.py" "$CLIENT/_vendor/sse.py"

# Format synced files to match project style
ruff format     "$MANSIO/types.py"     "$MANSIO/transport_http.py"     "$MANSIO/_vendor/httpclient.py"     "$MANSIO/_vendor/sse.py"     "$CLIENT/types.py"     "$CLIENT/transport.py"     "$CLIENT/_vendor/httpclient.py"     "$CLIENT/_vendor/sse.py"     2>/dev/null || true

# Stage synced files so pre-commit hook works like ruff-format
git add \
    "$MANSIO/types.py" "$CLIENT/types.py" \
    "$MANSIO/transport_http.py" "$CLIENT/transport.py" \
    "$MANSIO/_vendor/httpclient.py" "$CLIENT/_vendor/httpclient.py" \
    "$MANSIO/_vendor/sse.py" "$CLIENT/_vendor/sse.py" \
    2>/dev/null || true
