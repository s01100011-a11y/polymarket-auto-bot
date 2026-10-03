#!/bin/sh
set -eu

TS_SOCKET="${TS_SOCKET:-/tmp/tailscale/tailscaled.sock}"
TS_STATE_DIR="${TS_STATE_DIR:-/app/data/tailscale}"
TS_HOSTNAME="${TS_HOSTNAME:-railway-polymarket-bot}"
TS_PROXY_ADDR="${TS_PROXY_ADDR:-127.0.0.1:1055}"

mkdir -p "$(dirname "$TS_SOCKET")" "$TS_STATE_DIR"

echo "TAILSCALE_START userspace=true proxy=$TS_PROXY_ADDR state=$TS_STATE_DIR"
tailscaled \
  --tun=userspace-networking \
  --socket="$TS_SOCKET" \
  --state="$TS_STATE_DIR/tailscaled.state" \
  --socks5-server="$TS_PROXY_ADDR" \
  --outbound-http-proxy-listen="$TS_PROXY_ADDR" \
  >/tmp/tailscaled.log 2>&1 &
TS_PID=$!

i=0
while [ ! -S "$TS_SOCKET" ] && [ "$i" -lt 80 ]; do
  if ! kill -0 "$TS_PID" 2>/dev/null; then
    echo "TAILSCALE_ERROR tailscaled exited before socket was ready"
    cat /tmp/tailscaled.log || true
    exit 1
  fi
  i=$((i + 1))
  sleep 0.25
done

if [ ! -S "$TS_SOCKET" ]; then
  echo "TAILSCALE_ERROR socket not ready"
  cat /tmp/tailscaled.log || true
  exit 1
fi

if [ -z "${TS_AUTHKEY:-}" ]; then
  echo "TAILSCALE_ERROR TS_AUTHKEY is not set"
  exit 1
fi

if ! tailscale --socket="$TS_SOCKET" up \
  --auth-key="$TS_AUTHKEY" \
  --hostname="$TS_HOSTNAME" \
  --accept-dns=true; then
  echo "TAILSCALE_ERROR authentication failed"
  cat /tmp/tailscaled.log || true
  exit 1
fi

echo "TAILSCALE_READY"
tailscale --socket="$TS_SOCKET" ip -4 || true

# Repair the CFB poller scope regression and refresh existing live CFB state.
# This only updates matching/quote state; it never submits a bet.
if ! python -m app.cfb_runtime_hotfix; then
  echo "CFB_RUNTIME_HOTFIX_FAILED; continuing with normal startup"
fi

# Start the composite app with live CFB alternatives, explicit click/approval
# queue handling, and visible pending-approval controls on each CFB signal card.
exec python -m app.cfb_pending_approval_ui
