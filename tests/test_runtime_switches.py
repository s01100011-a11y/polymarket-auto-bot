import os
import unittest
from pathlib import Path
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

    def test_live_trading_has_no_cached_module_constant(self):
        self.assertFalse(hasattr(main, "LIVE_TRADING"))

    def test_runtime_live_switch_is_used_by_active_modules(self):
        app_dir = Path(__file__).resolve().parents[1] / "app"
        for filename in (
            "dashboard.py",
            "live_trading.py",
            "wallet_dashboard.py",
            "termux_executor_dashboard.py",
            "live_test_dashboard.py",
        ):
            with self.subTest(filename=filename):
                source = (app_dir / filename).read_text()
                self.assertNotIn("core.LIVE_TRADING", source)


if __name__ == "__main__":
    unittest.main()
