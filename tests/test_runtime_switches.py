import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import main


class RuntimeSwitchTests(unittest.TestCase):
    def test_auto_trading_runtime_truthy_values(self):
        for value in ("true", "TRUE", "1", "yes", "on", " true "):
            with self.subTest(value=value), patch.dict(os.environ, {"AUTO_TRADING": value}):
                self.assertTrue(main.auto_trading_enabled())

    def test_auto_trading_runtime_false(self):
        with patch.dict(os.environ, {"AUTO_TRADING": "false"}):
            self.assertFalse(main.auto_trading_enabled())

    def test_live_trading_runtime_truthy_values(self):
        for value in ("true", "TRUE", "1", "yes", "on", " true "):
            with self.subTest(value=value), patch.dict(os.environ, {"LIVE_TRADING": value}):
                self.assertTrue(main.live_trading_enabled())

    def test_live_trading_runtime_false(self):
        with patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            self.assertFalse(main.live_trading_enabled())

    def test_auto_switch_changes_without_module_reload(self):
        with patch.dict(os.environ, {"AUTO_TRADING": "false"}):
            self.assertFalse(main.auto_trading_enabled())
            os.environ["AUTO_TRADING"] = "true"
            self.assertTrue(main.auto_trading_enabled())

    def test_live_switch_changes_without_module_reload(self):
        with patch.dict(os.environ, {"LIVE_TRADING": "false"}):
            self.assertFalse(main.live_trading_enabled())
            os.environ["LIVE_TRADING"] = "true"
            self.assertTrue(main.live_trading_enabled())

    def test_watch_health_reports_recent_cycle_as_healthy(self):
        now = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)
        recent = now - timedelta(seconds=main.AUTO_POLL_SECONDS)
        with patch.dict(main.WATCH_HEALTH, {
            "last_started_at": recent.isoformat(),
            "last_completed_at": recent.isoformat(),
            "last_error": None,
            "cycles": 1,
        }, clear=True):
            status = main._watch_health_snapshot(now)
        self.assertTrue(status["healthy"])
        self.assertFalse(status["stale"])

    def test_watch_health_reports_stale_cycle_as_unhealthy(self):
        now = datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)
        old = now - timedelta(seconds=main.AUTO_POLL_SECONDS * 4)
        with patch.dict(main.WATCH_HEALTH, {
            "last_started_at": old.isoformat(),
            "last_completed_at": old.isoformat(),
            "last_error": None,
            "cycles": 1,
        }, clear=True):
            status = main._watch_health_snapshot(now)
        self.assertFalse(status["healthy"])
        self.assertTrue(status["stale"])


if __name__ == "__main__":
    unittest.main()
