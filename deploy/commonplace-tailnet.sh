#!/bin/sh
# launchd/systemd wrapper: bind the commonplace HTTP server to this machine's
# Tailscale IP, so the tailnet — and only the tailnet — can reach it.
# No app-level auth: Tailscale is the auth layer (see README → Security model).
#
# The IP is resolved at start rather than hardcoded, waiting for tailscaled
# to come up after boot.
set -eu

REPO=${COMMONPLACE_REPO:-$HOME/Documents/git/commonplace}
PORT=${COMMONPLACE_PORT:-9322}

if command -v tailscale >/dev/null 2>&1; then
    TAILSCALE=tailscale
else
    TAILSCALE=/Applications/Tailscale.app/Contents/MacOS/Tailscale
fi

HOST=""
for _ in $(seq 1 60); do
    HOST=$("$TAILSCALE" ip -4 2>/dev/null | head -n1) || true
    if [ -n "$HOST" ]; then
        break
    fi
    sleep 2
done
if [ -z "$HOST" ]; then
    echo "commonplace-tailnet: no Tailscale IP after 120s; is tailscaled running?" >&2
    exit 1
fi

exec uv run --directory "$REPO" commonplace serve --http --host "$HOST" --port "$PORT"
