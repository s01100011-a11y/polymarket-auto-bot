import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import dashboard_live_control_v4 as live_control
from app import termux_executor_dashboard as executor


class RailwayExecutionSafetyTests(unittest.TestCase):
    def test_waiting_approval_buy_expires(self):
        data = {
            "exec-test": {
                "id": "exec-test",
                "action": "BUY",
                "status": "WAITING_APPROVAL",
                "created_unix": 100.0,
            }
        }

        with patch.object(executor, "EXECUTOR_BUY_TTL_SECONDS", 180):
            expired = executor._expire_stale_buys(data, now=281.0)

        self.assertEqual(expired, ["exec-test"])
        self.assertEqual(data["exec-test"]["status"], "FAILED")
        self.assertIn("no order was submitted", data["exec-test"]["error"])

    def test_expired_legacy_lease_buy_expires(self):
        data = {
            "exec-test": {
                "id": "exec-test",
                "action": "BUY",
                "status": "LEASED",
                "created_unix": 100.0,
                "lease_until_unix": 200.0,
            }
        }

        with patch.object(executor, "EXECUTOR_BUY_TTL_SECONDS", 180):
            expired = executor._expire_stale_buys(data, now=281.0)

        self.assertEqual(expired, ["exec-test"])
        self.assertEqual(data["exec-test"]["status"], "FAILED")
        self.assertNotIn("lease_until_unix", data["exec-test"])

    def test_railway_status_does_not_require_remote_state(self):
        with (
            patch.dict(
                executor.os.environ,
                {
                    "POLYMARKET_PRIVATE_KEY": "configured",
                    "POLYMARKET_DEPOSIT_WALLET": "0x" + "1" * 40,
                },
                clear=False,
            ),
            patch.object(executor.core, "live_trading_enabled", return_value=True),
            patch.object(
                executor.core,
                "_check_geoblock",
                return_value={"blocked": False, "country": "XX", "region": "YY"},
            ),
            patch.object(
                executor,
                "_state",
                side_effect=AssertionError("remote state must not be read"),
            ),
        ):
            status = executor._railway_execution_status(refresh_geo=True)

        self.assertTrue(status["ready"])
        self.assertEqual(status["backend"], "railway")

    def test_remote_worker_polling_is_retired(self):
        with self.assertRaises(HTTPException) as ctx:
            executor.executor_next()
        self.assertEqual(ctx.exception.status_code, 410)

    def test_auto_buy_uses_local_railway_executor(self):
        done = {
            "id": "exec-test",
            "action": "BUY",
            "status": "DONE",
            "result": {"ok": True, "executor": "railway"},
        }
        with (
            patch.object(live_control.core, "auto_trading_enabled", return_value=True),
            patch.object(live_control.remote, "_enqueue", return_value=done) as enqueue,
            patch.object(
                live_control,
                "_executor_ready",
                side_effect=AssertionError("heartbeat readiness must not be consulted"),
            ),
        ):
            result = live_control._prepare_remote_buy(
                {"source": "slack_live", "auto": True}
            )

        self.assertEqual(result["status"], "DONE")
        enqueue.assert_called_once_with(
            "BUY", {"source": "slack_live", "auto": True}
        )


if __name__ == "__main__":
    unittest.main()
