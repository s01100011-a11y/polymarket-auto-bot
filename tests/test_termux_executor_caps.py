from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

from app import termux_executor_dashboard as remote

from scripts import termux_executor as executor


class TermuxExecutorTransportTests(unittest.TestCase):
    def test_bridge_client_forces_ipv4_with_retries(self):
        with (
            patch.object(executor.httpx, "HTTPTransport") as transport,
            patch.object(executor.httpx, "Client") as client,
        ):
            result = executor._bridge_client(timeout=17)

        transport.assert_called_once_with(local_address="0.0.0.0", retries=2)
        client.assert_called_once_with(transport=transport.return_value, timeout=17)
        self.assertIs(result, client.return_value)


class TermuxExecutorCapTests(unittest.TestCase):
    def test_budget_within_server_stamped_dashboard_cap_is_allowed(self):
        payload = {
            "budget_usdc": "30",
            "authorized_max_auto_trade_usdc": "50",
        }
        budget = executor._validate_budget_caps(payload)

        self.assertEqual(budget, Decimal("30"))

    def test_budget_above_server_stamped_dashboard_cap_is_blocked(self):
        payload = {
            "budget_usdc": "60",
            "authorized_max_auto_trade_usdc": "50",
        }
        with self.assertRaisesRegex(RuntimeError, "dashboard Auto trade cap"):
            executor._validate_budget_caps(payload)

    def test_missing_server_authorization_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "did not provide a valid dashboard Auto trade cap"):
            executor._validate_budget_caps({"budget_usdc": "10"})

    def test_no_phone_emergency_ceiling_when_server_cap_allows_budget(self):
        payload = {
            "budget_usdc": "600",
            "authorized_max_auto_trade_usdc": "1000",
        }
        budget = executor._validate_budget_caps(payload)

        self.assertEqual(budget, Decimal("600"))

    def test_worker_revision_uses_git_head(self):
        with patch.object(executor.subprocess, "check_output", return_value="abcdef123456\n") as check:
            self.assertEqual(executor._worker_revision(), "abcdef123456")
        check.assert_called_once()

    def test_worker_revision_fails_closed_to_unknown(self):
        with patch.object(executor.subprocess, "check_output", side_effect=OSError("git unavailable")):
            self.assertEqual(executor._worker_revision(), "unknown")

    def test_wallet_heartbeat_reports_queue_poll_telemetry(self):
        original = dict(executor.QUEUE_POLL_HEALTH)
        try:
            executor.QUEUE_POLL_HEALTH.update(
                {
                    "phase": "error",
                    "last_started_unix": 123.0,
                    "last_ok_unix": 120.0,
                    "last_http_status": None,
                    "last_error": "ConnectTimeout: timed out",
                    "consecutive_errors": 2,
                }
            )
            with patch.object(executor, "_secure", side_effect=RuntimeError("wallet test unavailable")):
                body = executor._wallet_heartbeat(
                    "private",
                    "0x12345678901234567890",
                    {"country": "MY", "region": "14", "blocked": False},
                    "Queue poll error",
                )
            self.assertEqual(body["queue_poll_phase"], "error")
            self.assertEqual(body["queue_poll_last_started_unix"], 123.0)
            self.assertEqual(body["queue_poll_last_ok_unix"], 120.0)
            self.assertEqual(body["queue_poll_last_error"], "ConnectTimeout: timed out")
            self.assertEqual(body["queue_poll_consecutive_errors"], 2)
        finally:
            executor.QUEUE_POLL_HEALTH.clear()
            executor.QUEUE_POLL_HEALTH.update(original)

    def test_heartbeat_accepts_revision_and_combo_capabilities(self):
        hb = remote.Heartbeat(
            name="termux-phone",
            worker_revision="abcdef123456",
            capabilities=["PREVIEW", "BUY", "SELL", "COMBO_PREVIEW", "COMBO_BUY"],
            queue_poll_phase="idle",
            queue_poll_last_started_unix=100.0,
            queue_poll_last_ok_unix=101.0,
            queue_poll_last_http_status=200,
            queue_poll_last_error=None,
            queue_poll_consecutive_errors=0,
        )
        self.assertEqual(hb.worker_revision, "abcdef123456")
        self.assertIn("COMBO_PREVIEW", hb.capabilities)
        self.assertIn("COMBO_BUY", hb.capabilities)
        self.assertEqual(hb.queue_poll_phase, "idle")
        self.assertEqual(hb.queue_poll_last_http_status, 200)


if __name__ == "__main__":
    unittest.main()
