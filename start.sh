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
sleep 1
tailscale --socket="$TS_SOCKET" ping --timeout=5s 100.81.244.65 || true

# Verify the private PW feeds through Tailscale's SOCKS5 proxy.
# SOCKS5 preserves the destination hostname for TLS/SNI without proxying
# unrelated Polymarket or Slack traffic.
python - <<'PY'
import os
import httpx

proxy = "socks5://127.0.0.1:1055"
since = os.getenv("PW_EXPORT_SINCE", "2026-08-01")
checks = [
    ("WNBA", os.getenv("PW_WNBA_EXPORT_URL", "https://bob-mbp-ubuntu.taila35415.ts.net:8445/api/pw-export")),
    ("NBA", os.getenv("PW_NBA_EXPORT_URL", "https://bob-mbp-ubuntu.taila35415.ts.net:8444/api/pw-export")),
]
for name, url in checks:
    try:
        with httpx.Client(proxy=proxy, timeout=10.0, follow_redirects=True) as client:
            r = client.get(url, params={"source": "live", "since": since})
        msg = f"TAILSCALE_PW_CHECK sport={name} status={r.status_code}"
        if r.status_code >= 400:
            msg += " body=" + r.text[:180].replace("\n", " ")
        else:
            msg += f" bytes={len(r.content)}"
        print(msg)
    except Exception as exc:
        print(f"TAILSCALE_PW_CHECK sport={name} error={type(exc).__name__}: {exc}")
PY

exec uvicorn app.wnba_pw_strategy_test_v12:app --host 0.0.0.0 --port "${PORT:-8080}"
