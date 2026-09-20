#!/usr/bin/env python3
"""Polling loop wrapper for monitor.py — replaces systemd timer on ChromeOS/Crostini.

Usage:
    python3 monitor_loop.py

Runs monitor.py every POLL_INTERVAL seconds (default 30).
Designed for use with supervisord on systems without systemd.

See: #299
"""

import os
import subprocess
import sys
import time

POLL_INTERVAL = int(os.environ.get("MONITOR_POLL_INTERVAL", "30"))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MONITOR_PY = os.path.join(SCRIPT_DIR, "monitor.py")
PYTHON = sys.executable


def main():
    print(f"monitor_loop: starting (interval={POLL_INTERVAL}s)")
    while True:
        try:
            subprocess.run([PYTHON, MONITOR_PY], cwd=SCRIPT_DIR)
        except Exception as e:
            print(f"monitor_loop: error running monitor.py: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
