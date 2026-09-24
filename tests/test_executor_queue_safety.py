import unittest
from decimal import Decimal
from unittest.mock import patch

from fastapi import HTTPException

from app import dashboard_live_control_v4 as live_control
from app import termux_executor_dashboard as remote


class ExecutorQueueSafetyTests(unittest.TestCase):
    def test_pending_buy_expires(self):
        data = {
            "exec-test": {
                "id": "exec-test",
                "action": "BUY",
                "status": "PENDING",
                "created_unix": 100.0,
            }
        }

        with patch.object(remote, "EXECUTOR_BUY_TTL_SECONDS", 180):
            expired = remote._expire_stale_buys(data, now=281.0)

        self.assertEqual(expired, ["exec-test"])
        self.assertEqual(data["exec-test"]["status"], "FAILED")
        self.assertIn("no order was submitted", data["exec-test"]["error"])

    def test_expired_lease_buy_expires(self):
        data = {
            "exec-test": {
                "id": "exec-test",
                "action": "BUY",
                "status": "LEASED",
                "created_unix": 100.0,
                "lease_until_unix": 200.0,  # lease already expired
            }
        }

        with patch.object(remote, "EXECUTOR_BUY_TTL_SECONDS", 180):
            expired = remote._expire_stale_buys(data, now=281.0)

        self.assertEqual(expired, ["exec-test"])
        self.assertEqual(data["exec-test"]["status"], "FAILED")
        self.assertIn("Reconcile the wallet", data["exec-test"]["error"])
        self.assertNotIn("lease_until_unix", data["exec-test"])

    def test_active_lease_is_not_expired(self):
        data = {
            "exec-test": {
                "id": "exec-test",
                "action": "BUY",
                "status": "LEASED",
                "created_unix": 100.0,
                "lease_until_unix": 300.0,
            }
        }

        with patch.object(remote, "EXECUTOR_BUY_TTL_SECONDS", 180):
            expired = remote._expire_stale_buys(data, now=281.0)

        self.assertEqual(expired, [])
        self.assertEqual(data["exec-test"]["status"], "LEASED")

    def test_handoff_stamps_current_dashboard_cap(self):
        rec = {
            "id": "exec-test",
            "action": "BUY",
            "status": "PENDING",
            "payload": {"budget_usdc": "30"},
        }
        with patch.object(remote.core, "MAX_AUTO_TRADE_USDC", Decimal("50")):
            allowed = remote._authorize_order_for_handoff(rec)

        self.assertTrue(allowed)
        self.assertEqual(rec["payload"]["authorized_max_auto_trade_usdc"], "50")
        self.assertEqual(rec["status"], "PENDING")

    def test_handoff_blocks_buy_above_reduced_dashboard_cap(self):
        rec = {
            "id": "exec-test",
            "action": "BUY",
            "status": "PENDING",
            "payload": {
                "budget_usdc": "30",
                "authorized_max_auto_trade_usdc": "100",
            },
        }
        with patch.object(remote.core, "MAX_AUTO_TRADE_USDC", Decimal("20")):
            allowed = remote._authorize_order_for_handoff(rec)

        self.assertFalse(allowed)
        self.assertEqual(rec["status"], "FAILED")
        self.assertIn("current dashboard Auto trade cap $20", rec["error"])
        self.assertEqual(rec["payload"]["authorized_max_auto_trade_usdc"], "100")

    def test_remote_buy_rejected_when_executor_offline(self):
        with (
            patch.object(
                remote,
                "_state",
                return_value={
                    "token_hash": "paired",
                    "last_seen_unix": 0,
                    "geo_blocked": False,
                },
            ),
            self.assertRaises(HTTPException),
        ):
            remote._enqueue("BUY", {"trade_id": "test"})

    def test_auto_buy_not_prepared_when_executor_offline(self):
        with (
            patch.object(
                live_control.core,
                "auto_trading_enabled",
                return_value=True,
            ),
            patch.object(
                live_control,
                "_executor_ready",
                return_value=(False, {"geo_blocked": False}),
            ),
            self.assertRaisesRegex(ValueError, "offline"),
        ):
            live_control._prepare_remote_buy({"source": "slack_live"})


if __name__ == "__main__":
    unittest.main()
