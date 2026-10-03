#!/bin/sh
set -eu

# Preserve the totals-remote startup/Tailscale bootstrap, replacing only the
# final Python launcher so the attribution + SH01 + unit-P/L layer remains live.
sed 's/app\.totals_remote_launcher/app.totals_remote_attribution_launcher/' /app/start_totals_remote.sh > /tmp/start-totals-attribution-runtime.sh
exec /bin/sh /tmp/start-totals-attribution-runtime.sh
