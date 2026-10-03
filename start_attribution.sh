#!/bin/sh
set -eu

# Reuse the normal startup/Tailscale bootstrap unchanged, replacing only the
# final Python module so all existing network/session behavior is preserved.
sed 's/app\.cfb_exact_position_fix_v3/app.dashboard_attribution_v1/' /app/start.sh > /tmp/start-attribution-runtime.sh
exec /bin/sh /tmp/start-attribution-runtime.sh
