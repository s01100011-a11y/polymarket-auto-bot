from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

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
        with patch.object(executor, "EMERGENCY_MAX_USDC", Decimal("500")):
            budget = executor._validate_budget_caps(payload)

        self.assertEqual(budget, Decimal("30"))

    def test_budget_above_server_stamped_dashboard_cap_is_blocked(self):
        payload = {
            "budget_usdc": "60",
            "authorized_max_auto_trade_usdc": "50",
        }
        with (
            patch.object(executor, "EMERGENCY_MAX_USDC", Decimal("500")),
            self.assertRaisesRegex(RuntimeError, "dashboard Auto trade cap"),
        ):
            executor._validate_budget_caps(payload)

    def test_missing_server_authorization_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "did not provide a valid dashboard Auto trade cap"):
            executor._validate_budget_caps({"budget_usdc": "10"})

    def test_emergency_ceiling_remains_independent_backstop(self):
        payload = {
            "budget_usdc": "600",
            "authorized_max_auto_trade_usdc": "1000",
        }
        with (
            patch.object(executor, "EMERGENCY_MAX_USDC", Decimal("500")),
            self.assertRaisesRegex(RuntimeError, "emergency hard ceiling"),
        ):
            executor._validate_budget_caps(payload)


if __name__ == "__main__":
    unittest.main()
