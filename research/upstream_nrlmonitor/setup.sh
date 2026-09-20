#!/bin/bash
# NRL Monitor Setup Script
# Run once to set up, or re-run to reset/reinstall.
# Usage: bash /path/to/nrl-monitor/setup.sh
#
# Prerequisites:
#   - config.json must exist (copy from config.example.json and fill in your values)
#   - Python 3.9+

set -e

# Resolve script directory regardless of where it's called from
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== NRL Monitor Setup ==="
echo "Working directory: $SCRIPT_DIR"
echo ""

# 1. Create logs dir and ensure monitor.py is executable
mkdir -p "$SCRIPT_DIR/logs"
chmod +x "$SCRIPT_DIR/monitor.py"
echo "✅ Logs directory ready"

# 2. Check config.json exists
if [ ! -f "$SCRIPT_DIR/config.json" ]; then
    echo ""
    echo "❌ config.json not found."
    echo "   Copy the example and fill in your values:"
    echo "   cp $SCRIPT_DIR/config.example.json $SCRIPT_DIR/config.json"
    echo "   Then edit: slack_bot_token, slack_channel"
    exit 1
fi
echo "✅ config.json found"

# 3. Install Python requests if needed
if ! python3 -c "import requests" 2>/dev/null; then
    echo "Installing python3-requests..."
    pip3 install requests --quiet || python3 -m pip install requests --quiet
fi
echo "✅ Python requests available"

# 4. Test NRL API
echo ""
echo "Testing NRL API..."
python3 -c "
import json, urllib.request
url = 'https://www.nrl.com/draw/data?competition=111&season=2026&round=1'
headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
req = urllib.request.Request(url, headers=headers)
with urllib.request.urlopen(req, timeout=10) as r:
    data = json.loads(r.read())
fixtures = data.get('fixtures', [])
print(f'  Fixtures in R1: {len(fixtures)}')
for f in fixtures[:3]:
    h = f.get('homeTeam', {}).get('nickName', '?')
    a = f.get('awayTeam', {}).get('nickName', '?')
    s = f.get('matchState', '?')
    print(f'    {h} vs {a} — {s}')
"
echo "✅ NRL API working"

# 5. Scheduler setup
echo ""
echo "How would you like to schedule the monitor?"
echo "  1) systemd user timer (recommended)"
echo "  2) cron"
echo "  3) skip scheduler setup"
read -p "Choice [1/2/3]: " choice

case "$choice" in
    1)
        echo ""
        echo "Installing systemd user timer..."
        mkdir -p ~/.config/systemd/user

        # Service
        cat > ~/.config/systemd/user/nrl-monitor.service <<EOF
[Unit]
Description=NRL Monitor — one-shot polling run
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$SCRIPT_DIR
ExecStart=/usr/bin/python3 $SCRIPT_DIR/monitor.py
StandardOutput=append:$SCRIPT_DIR/logs/monitor.log
StandardError=append:$SCRIPT_DIR/logs/monitor.log
EOF

        # Timer — NRL games typically Thu 19:50 to Mon 22:00 AEST
        # AEST = UTC+10, so Thu 09:00 UTC to Mon 12:00 UTC covers all games
        cat > ~/.config/systemd/user/nrl-monitor.timer <<EOF
[Unit]
Description=NRL Monitor timer — poll every minute during game windows

[Timer]
OnCalendar=*-*-* 06,07,08,09,10,11,12:*:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
EOF

        # Dashboard
        cat > ~/.config/systemd/user/nrl-dashboard.service <<EOF
[Unit]
Description=NRL Monitor Dashboard
After=network.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
ExecStart=/usr/bin/python3 $SCRIPT_DIR/server.py 8898
StandardOutput=append:$SCRIPT_DIR/logs/dashboard.log
StandardError=append:$SCRIPT_DIR/logs/dashboard.log
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

        systemctl --user daemon-reload
        systemctl --user enable --now nrl-monitor.timer
        systemctl --user enable --now nrl-dashboard.service
        echo "✅ systemd timer and dashboard installed and started"
        echo "   Timer: systemctl --user status nrl-monitor.timer"
        echo "   Dashboard: http://127.0.0.1:8899/dashboard.html"
        ;;
    2)
        echo ""
        echo "Adding cron entries..."
        # Remove existing nrl-monitor entries
        crontab -l 2>/dev/null | grep -v "nrl-monitor" | grep -v "server.py 8898" > /tmp/crontab.tmp || true
        # Add new entries — poll every minute 06:00-12:00 UTC (covers AEST evening games)
        echo "* 6-12 * * * cd $SCRIPT_DIR && python3 monitor.py >> logs/monitor.log 2>&1" >> /tmp/crontab.tmp
        echo "@reboot nohup python3 $SCRIPT_DIR/server.py 8898 > $SCRIPT_DIR/logs/dashboard.log 2>&1 &" >> /tmp/crontab.tmp
        crontab /tmp/crontab.tmp
        rm /tmp/crontab.tmp
        echo "✅ Cron entries installed"
        ;;
    3)
        echo "Skipping scheduler setup."
        ;;
esac

echo ""
echo "=== Setup Complete ==="
echo "  Run manually:  python3 $SCRIPT_DIR/monitor.py"
echo "  Dashboard:     python3 $SCRIPT_DIR/server.py 8898"
echo "                 → http://127.0.0.1:8899/dashboard.html"
