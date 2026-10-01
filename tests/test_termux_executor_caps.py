from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app import termux_executor_dashboard as remote

from scripts import termux_executor as executor


class TermuxExecutorSingletonTests(unittest.TestCase):
    def test_worker_lock_allows_only_one_executor_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = executor.Path(tmp) / "executor.lock"
            with patch.object(executor, "WORKER_LOCK_FILE", lock_path):
                first = executor._acquire_worker_lock()
                self.assertIsNotNone(first)
                try:
                    second = executor._acquire_worker_lock()
                    self.assertIsNone(second)
                finally:
                    first.close()

                third = executor._acquire_worker_lock()
                self.assertIsNotNone(third)
                third.close()


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


class TermuxExecutorBuyRetryTests(unittest.TestCase):
    def _quote(self):
        return {
            "asset_id": "asset-1",
            "requested_shares": "10",
            "max_price": "0.50",
            "limit_order_ttl_seconds": 0,
        }

    def test_buy_retries_connect_timeout_during_preflight(self):
        response = type("Order", (), {"order_id": "order-1"})()
        secure = MagicMock()
        secure.return_value.__enter__.return_value.place_limit_order.return_value = response
        with (
            patch.object(
                executor,
                "_validate_buy",
                side_effect=[executor.httpx.ConnectTimeout("timed out"), self._quote()],
            ) as validate,
            patch.object(executor, "_position_size", side_effect=[Decimal("0"), Decimal("10")]),
            patch.object(executor, "_secure", secure),
            patch.object(executor.time, "sleep", return_value=None),
        ):
            result = executor._buy({"trade_id": "retry-preflight"}, "private", "wallet")

        self.assertTrue(result["ok"])
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(
            secure.return_value.__enter__.return_value.place_limit_order.call_count,
            1,
        )

    def test_buy_retries_connect_timeout_during_submission(self):
        response = type("Order", (), {"order_id": "order-2"})()
        secure = MagicMock()
        secure.return_value.__enter__.return_value.place_limit_order.side_effect = [
            executor.httpx.ConnectTimeout("timed out"),
            response,
        ]
        with (
            patch.object(executor, "_validate_buy", return_value=self._quote()),
            patch.object(executor, "_position_size", side_effect=[Decimal("0"), Decimal("10")]),
            patch.object(executor, "_secure", secure),
            patch.object(executor.time, "sleep", return_value=None),
        ):
            result = executor._buy({"trade_id": "retry-submit"}, "private", "wallet")

        self.assertTrue(result["ok"])
        self.assertEqual(
            secure.return_value.__enter__.return_value.place_limit_order.call_count,
            2,
        )

    def test_buy_does_not_retry_ambiguous_read_timeout_during_submission(self):
        secure = MagicMock()
        secure.return_value.__enter__.return_value.place_limit_order.side_effect = (
            executor.httpx.ReadTimeout("timed out")
        )
        with (
            patch.object(executor, "_validate_buy", return_value=self._quote()),
            patch.object(executor, "_position_size", return_value=Decimal("0")),
            patch.object(executor, "_secure", secure),
            patch.object(executor.time, "sleep", return_value=None),
            self.assertRaisesRegex(RuntimeError, "BUY_SUBMISSION_AMBIGUOUS_TIMEOUT"),
        ):
            executor._buy({"trade_id": "ambiguous-submit"}, "private", "wallet")

        self.assertEqual(
            secure.return_value.__enter__.return_value.place_limit_order.call_count,
            1,
        )

    def test_buy_never_resubmits_after_submission_when_reconcile_times_out(self):
        response = type("Order", (), {"order_id": "order-3"})()
        secure = MagicMock()
        secure.return_value.__enter__.return_value.place_limit_order.return_value = response

        position_calls = [Decimal("0")] + [
            executor.httpx.ConnectTimeout("timed out")
            for _ in range(
                (executor.BUY_CONNECT_RETRIES + 1)
                * executor.BUY_POSITION_RECONCILE_ATTEMPTS
            )
        ]
        with (
            patch.object(executor, "_validate_buy", return_value=self._quote()),
            patch.object(executor, "_position_size", side_effect=position_calls),
            patch.object(executor, "_secure", secure),
            patch.object(executor.time, "sleep", return_value=None),
            self.assertRaisesRegex(RuntimeError, "not re-submitted"),
        ):
            executor._buy({"trade_id": "no-duplicate"}, "private", "wallet")

        self.assertEqual(
            secure.return_value.__enter__.return_value.place_limit_order.call_count,
            1,
        )


class SlackLiveRestingLimitTests(unittest.TestCase):
    def test_fast_slack_preflight_skips_public_network_validation(self):
        payload = {
            "source": "slack_live",
            "server_fast_preflight": True,
            "server_fast_preflight_unix": executor.time.time(),
            "server_fast_preflight_max_age_seconds": 45,
            "server_position_before": "0",
            "market_url": "https://polymarket.com/sports/wnba/test-event",
            "market_label": "Dallas Wings vs Test",
            "outcome": "Dallas Wings",
            "market_type": "moneyline",
            "asset_id": "asset-1",
            "max_price": "0.40",
            "signal_buy_price": "0.40",
            "signal_spread": "0.02",
            "budget_usdc": "10",
            "max_price_global": "0.95",
            "authorized_max_auto_trade_usdc": "25",
            "limit_order_ttl_seconds": 120,
        }
        with (
            patch.object(executor, "_geo") as geo,
            patch.object(executor, "PublicClient") as public,
            patch.object(executor, "_position_size") as position,
        ):
            quote = executor._fast_slack_preflight(payload)
        self.assertIsNotNone(quote)
        self.assertEqual(quote["asset_id"], "asset-1")
        self.assertEqual(quote["server_position_before"], "0")
        geo.assert_not_called()
        public.assert_not_called()
        position.assert_not_called()

    def test_fast_slack_preflight_rejects_stale_server_authorization(self):
        payload = {
            "source": "slack_live",
            "server_fast_preflight": True,
            "server_fast_preflight_unix": executor.time.time() - 90,
            "server_fast_preflight_max_age_seconds": 45,
            "market_url": "https://polymarket.com/sports/wnba/test-event",
            "outcome": "Dallas Wings",
            "market_type": "moneyline",
            "asset_id": "asset-1",
            "max_price": "0.40",
            "budget_usdc": "10",
            "max_price_global": "0.95",
            "authorized_max_auto_trade_usdc": "25",
            "limit_order_ttl_seconds": 120,
        }
        with self.assertRaisesRegex(RuntimeError, "stale"):
            executor._fast_slack_preflight(payload)

    def test_slack_live_defaults_to_120_second_resting_limit(self):
        self.assertEqual(
            executor._limit_order_seconds({"source": "slack_live"}),
            executor.SLACK_LIVE_LIMIT_ORDER_SECONDS,
        )
        self.assertEqual(executor.SLACK_LIVE_LIMIT_ORDER_SECONDS, 120)

    def test_explicit_limit_ttl_is_bounded(self):
        self.assertEqual(executor._limit_order_seconds({"limit_order_ttl_seconds": "120"}), 120)
        self.assertEqual(executor._limit_order_seconds({"limit_order_ttl_seconds": "9999"}), 300)

    def test_live_limit_can_rest_when_ask_is_above_target(self):
        market = MagicMock()
        market.question = "Test"
        market.trading.minimum_order_size = "1"
        market_type = patch.object(executor.core, "_market_type", return_value="moneyline")
        public = MagicMock()
        with (
            patch.object(executor, "_geo", return_value={"blocked": False}),
            patch.object(executor, "PublicClient", public),
            patch.object(executor, "_asset_market", return_value=(market, "Dallas Wings")),
            market_type,
        ):
            quote = executor._validate_buy({
                "source": "slack_live",
                "market_url": "https://polymarket.com/sports/wnba/test-event",
                "outcome": "Dallas Wings",
                "market_type": "moneyline",
                "asset_id": "asset-1",
                "max_price": "0.40",
                "signal_buy_price": "0.40",
                "signal_spread": "0.02",
                "budget_usdc": "10",
                "max_spread": "0.08",
                "max_price_global": "0.95",
                "authorized_max_auto_trade_usdc": "25",
                "limit_order_ttl_seconds": 120,
            })
        self.assertTrue(quote["will_rest_if_needed"])
        self.assertEqual(quote["max_price"], "0.40")
        self.assertEqual(quote["limit_order_ttl_seconds"], 120)
        public.return_value.__enter__.return_value.get_price.assert_not_called()
        public.return_value.__enter__.return_value.get_spread.assert_not_called()
        public.return_value.__enter__.return_value.get_order_book.assert_not_called()

    def test_live_limit_lease_covers_120_second_rest_window(self):
        rec = {
            "action": "BUY",
            "payload": {"limit_order_ttl_seconds": 120},
        }
        self.assertGreaterEqual(remote._lease_seconds_for(rec), 240)


class SlackFailedBuyRetryTests(unittest.TestCase):
    def test_failed_preflight_connect_timeout_code_is_retryable(self):
        from app import dashboard_live_control_v4 as live_control

        original_id = "exec-preflight-timeout"
        original = {
            "id": original_id,
            "action": "BUY",
            "status": "FAILED",
            "error": "RuntimeError: BUY_PREFLIGHT_CONNECT_TIMEOUT after 3 attempts: timed out",
            "payload": {
                "source": "slack_live",
                "market_url": "https://polymarket.com/sports/wnba/test-event",
                "outcome": "Dallas Wings",
                "market_type": "moneyline",
                "max_price": "0.40",
                "budget_usdc": "10",
                "trade_id": "slack-live-test",
            },
        }
        store = {original_id: original}

        with (
            patch.object(live_control.core, "bot_enabled", return_value=True),
            patch.object(live_control.core, "live_trading_enabled", return_value=True),
            patch.object(live_control.core, "auto_trading_enabled", return_value=True),
            patch.object(live_control, "_executor_ready", return_value=(True, {})),
            patch.object(live_control, "_retry_asset_id", return_value="asset-1"),
            patch.object(live_control.remote, "_authoritative_position", return_value=None),
            patch.object(live_control, "_open_orders_for_asset", return_value=[]),
            patch.object(live_control, "_active_or_pending", return_value=False),
            patch.object(live_control.remote, "_queue_load", side_effect=lambda: store),
            patch.object(live_control.remote, "_queue_save", side_effect=lambda data: store.update(data)),
        ):
            result = live_control._retry_failed_slack_buy_once(original_id)

        self.assertEqual(result["status"], "queued")
        queued = store[result["request_id"]]
        self.assertEqual(queued["payload"]["limit_order_ttl_seconds"], 120)
        self.assertEqual(queued["payload"]["asset_id"], "asset-1")

    def test_explicit_transport_timeout_slack_buy_requeues_after_reconciliation(self):
        from app import dashboard_live_control_v4 as live_control

        original_id = "exec-transport-timeout"
        original = {
            "id": original_id,
            "action": "BUY",
            "status": "FAILED",
            "error": "TransportError: timed out",
            "payload": {
                "source": "slack_live",
                "market_url": "https://polymarket.com/sports/wnba/test-event",
                "outcome": "Dallas Wings",
                "market_type": "moneyline",
                "max_price": "0.40",
                "budget_usdc": "10",
                "trade_id": "slack-live-test",
            },
        }
        store = {original_id: original}

        with (
            patch.object(live_control.core, "bot_enabled", return_value=True),
            patch.object(live_control.core, "live_trading_enabled", return_value=True),
            patch.object(live_control.core, "auto_trading_enabled", return_value=True),
            patch.object(live_control, "_executor_ready", return_value=(True, {})),
            patch.object(live_control, "_retry_asset_id", return_value="asset-1"),
            patch.object(live_control.remote, "_authoritative_position", return_value=None),
            patch.object(live_control, "_open_orders_for_asset", return_value=[]),
            patch.object(live_control, "_active_or_pending", return_value=False),
            patch.object(live_control.remote, "_queue_load", side_effect=lambda: store),
            patch.object(live_control.remote, "_queue_save", side_effect=lambda data: store.update(data)),
        ):
            result = live_control._retry_failed_slack_buy_once(original_id)

        self.assertEqual(result["status"], "queued")
        queued = store[result["request_id"]]
        self.assertEqual(queued["payload"]["limit_order_ttl_seconds"], 120)
        self.assertEqual(queued["payload"]["asset_id"], "asset-1")

    def test_failed_connect_timeout_slack_buy_requeues_once(self):
        from app import dashboard_live_control_v4 as live_control

        original_id = "exec-original"
        original = {
            "id": original_id,
            "action": "BUY",
            "status": "FAILED",
            "error": "ConnectTimeout: timed out",
            "payload": {
                "source": "slack_live",
                "market_url": "https://polymarket.com/sports/wnba/test-event",
                "outcome": "Dallas Wings",
                "market_type": "moneyline",
                "max_price": "0.40",
                "budget_usdc": "10",
                "trade_id": "slack-live-test",
            },
        }
        store = {original_id: original}

        with (
            patch.object(live_control.core, "bot_enabled", return_value=True),
            patch.object(live_control.core, "live_trading_enabled", return_value=True),
            patch.object(live_control.core, "auto_trading_enabled", return_value=True),
            patch.object(live_control, "_executor_ready", return_value=(True, {})),
            patch.object(live_control, "_retry_asset_id", return_value="asset-1"),
            patch.object(live_control.remote, "_authoritative_position", return_value=None),
            patch.object(live_control, "_open_orders_for_asset", return_value=[]),
            patch.object(live_control, "_active_or_pending", return_value=False),
            patch.object(live_control.remote, "_queue_load", side_effect=lambda: store),
            patch.object(live_control.remote, "_queue_save", side_effect=lambda data: store.update(data)),
        ):
            result = live_control._retry_failed_slack_buy_once(original_id)
            again = live_control._retry_failed_slack_buy_once(original_id)

        self.assertEqual(result["status"], "queued")
        self.assertEqual(again["status"], "already_retried")
        self.assertEqual(store[original_id]["manual_retry_request_id"], result["request_id"])
        self.assertEqual(store[result["request_id"]]["payload"]["retry_of_request_id"], original_id)

    def test_failed_slack_buy_does_not_requeue_when_position_exists(self):
        from app import dashboard_live_control_v4 as live_control

        original_id = "exec-original"
        store = {
            original_id: {
                "id": original_id,
                "action": "BUY",
                "status": "FAILED",
                "error": "ConnectTimeout: timed out",
                "payload": {
                    "source": "slack_live",
                    "market_url": "https://polymarket.com/sports/wnba/test-event",
                    "outcome": "Dallas Wings",
                    "market_type": "moneyline",
                    "max_price": "0.40",
                    "budget_usdc": "10",
                    "trade_id": "slack-live-test",
                },
            }
        }
        position = type("Position", (), {"current_size": "12.5"})()
        with (
            patch.object(live_control.core, "bot_enabled", return_value=True),
            patch.object(live_control.core, "live_trading_enabled", return_value=True),
            patch.object(live_control.core, "auto_trading_enabled", return_value=True),
            patch.object(live_control, "_executor_ready", return_value=(True, {})),
            patch.object(live_control, "_retry_asset_id", return_value="asset-1"),
            patch.object(live_control.remote, "_authoritative_position", return_value=position),
            patch.object(live_control.remote, "_queue_load", return_value=store),
        ):
            result = live_control._retry_failed_slack_buy_once(original_id)

        self.assertEqual(result["status"], "position_exists")
        self.assertNotIn("manual_retry_request_id", store[original_id])


    def test_interrupted_slack_buy_blocks_when_open_order_exists(self):
        from app import dashboard_live_control_v4 as live_control

        request_id = "exec-interrupted"
        store = {
            request_id: {
                "id": request_id,
                "action": "BUY",
                "status": "FAILED",
                "error": (
                    "This trade request was interrupted after execution began. "
                    "Automatic retry was blocked to prevent a duplicate order; "
                    "reconcile the wallet position manually."
                ),
                "payload": {
                    "source": "slack_live",
                    "market_url": "https://polymarket.com/sports/wnba/test-event",
                    "outcome": "Dallas Wings",
                    "market_type": "moneyline",
                    "max_price": "0.40",
                    "budget_usdc": "10",
                    "trade_id": "slack-live-test-retry",
                },
            }
        }
        order = type("OpenOrder", (), {"order_id": "live-order-1"})()
        with (
            patch.object(live_control.core, "bot_enabled", return_value=True),
            patch.object(live_control.core, "live_trading_enabled", return_value=True),
            patch.object(live_control.core, "auto_trading_enabled", return_value=True),
            patch.object(live_control, "_executor_ready", return_value=(True, {})),
            patch.object(live_control, "_retry_asset_id", return_value="asset-1"),
            patch.object(live_control.remote, "_authoritative_position", return_value=None),
            patch.object(live_control, "_open_orders_for_asset", return_value=[order]),
            patch.object(live_control.remote, "_queue_load", return_value=store),
        ):
            result = live_control._retry_failed_slack_buy_once(request_id)

        self.assertEqual(result["status"], "open_order_exists")
        self.assertEqual(result["order_ids"], ["live-order-1"])
        self.assertNotIn("manual_retry_request_id", store[request_id])

    def test_interrupted_slack_buy_requeues_only_after_empty_reconciliation(self):
        from app import dashboard_live_control_v4 as live_control

        request_id = "exec-interrupted"
        store = {
            request_id: {
                "id": request_id,
                "action": "BUY",
                "status": "FAILED",
                "error": (
                    "This trade request was interrupted after execution began. "
                    "Automatic retry was blocked to prevent a duplicate order; "
                    "reconcile the wallet position manually."
                ),
                "payload": {
                    "source": "slack_live",
                    "market_url": "https://polymarket.com/sports/wnba/test-event",
                    "outcome": "Dallas Wings",
                    "market_type": "moneyline",
                    "max_price": "0.40",
                    "budget_usdc": "10",
                    "trade_id": "slack-live-test-retry",
                },
            }
        }
        with (
            patch.object(live_control.core, "bot_enabled", return_value=True),
            patch.object(live_control.core, "live_trading_enabled", return_value=True),
            patch.object(live_control.core, "auto_trading_enabled", return_value=True),
            patch.object(live_control, "_executor_ready", return_value=(True, {})),
            patch.object(live_control, "_retry_asset_id", return_value="asset-1"),
            patch.object(live_control.remote, "_authoritative_position", return_value=None),
            patch.object(live_control, "_open_orders_for_asset", return_value=[]),
            patch.object(live_control, "_active_or_pending", return_value=False),
            patch.object(live_control.remote, "_queue_load", side_effect=lambda: store),
            patch.object(live_control.remote, "_queue_save", side_effect=lambda data: store.update(data)),
        ):
            result = live_control._retry_failed_slack_buy_once(request_id)

        self.assertEqual(result["status"], "queued")
        queued = store[result["request_id"]]
        self.assertEqual(
            queued["payload"]["retry_reason"],
            "manual_resend_after_interrupted_execution_reconciled",
        )

    def test_interrupted_slack_buy_fails_closed_when_open_order_check_errors(self):
        from app import dashboard_live_control_v4 as live_control

        request_id = "exec-interrupted"
        store = {
            request_id: {
                "id": request_id,
                "action": "BUY",
                "status": "FAILED",
                "error": "This trade request was interrupted after execution began.",
                "payload": {
                    "source": "slack_live",
                    "market_url": "https://polymarket.com/sports/wnba/test-event",
                    "outcome": "Dallas Wings",
                    "market_type": "moneyline",
                    "max_price": "0.40",
                    "budget_usdc": "10",
                    "trade_id": "slack-live-test-retry",
                },
            }
        }
        with (
            patch.object(live_control.core, "bot_enabled", return_value=True),
            patch.object(live_control.core, "live_trading_enabled", return_value=True),
            patch.object(live_control.core, "auto_trading_enabled", return_value=True),
            patch.object(live_control, "_executor_ready", return_value=(True, {})),
            patch.object(live_control, "_retry_asset_id", return_value="asset-1"),
            patch.object(live_control.remote, "_authoritative_position", return_value=None),
            patch.object(
                live_control,
                "_open_orders_for_asset",
                side_effect=RuntimeError("order query unavailable"),
            ),
            patch.object(live_control.remote, "_queue_load", return_value=store),
        ):
            result = live_control._retry_failed_slack_buy_once(request_id)

        self.assertEqual(result["status"], "open_order_check_failed")
        self.assertNotIn("manual_retry_request_id", store[request_id])


class SlackFastHandoffAuthorizationTests(unittest.TestCase):
    def _rec(self):
        return {
            "id": "exec-live",
            "action": "BUY",
            "status": "PENDING",
            "payload": {
                "source": "slack_live",
                "asset_id": "asset-1",
                "budget_usdc": "10",
                "limit_order_ttl_seconds": 120,
            },
        }

    def test_handoff_stamps_fast_preflight_after_empty_reconciliation(self):
        rec = self._rec()
        with (
            patch.object(remote.core, "MAX_AUTO_TRADE_USDC", Decimal("25")),
            patch.object(
                remote,
                "_state",
                return_value={
                    "last_seen_unix": remote.time.time(),
                    "geo_blocked": False,
                },
            ),
            patch.object(remote, "_strict_position_size", return_value=Decimal("0")),
            patch.object(remote, "_strict_open_orders_for_asset", return_value=[]),
        ):
            allowed = remote._authorize_order_for_handoff(rec)

        self.assertTrue(allowed)
        payload = rec["payload"]
        self.assertTrue(payload["server_fast_preflight"])
        self.assertEqual(payload["server_position_before"], "0")
        self.assertEqual(payload["authorized_max_auto_trade_usdc"], "25")

    def test_handoff_blocks_fast_preflight_when_position_exists(self):
        rec = self._rec()
        with (
            patch.object(remote.core, "MAX_AUTO_TRADE_USDC", Decimal("25")),
            patch.object(
                remote,
                "_state",
                return_value={
                    "last_seen_unix": remote.time.time(),
                    "geo_blocked": False,
                },
            ),
            patch.object(remote, "_strict_position_size", return_value=Decimal("5")),
            patch.object(remote, "_strict_open_orders_for_asset", return_value=[]),
        ):
            allowed = remote._authorize_order_for_handoff(rec)

        self.assertFalse(allowed)
        self.assertEqual(rec["status"], "FAILED")
        self.assertIn("already holds", rec["error"])

    def test_handoff_blocks_fast_preflight_when_open_order_exists(self):
        rec = self._rec()
        with (
            patch.object(remote.core, "MAX_AUTO_TRADE_USDC", Decimal("25")),
            patch.object(
                remote,
                "_state",
                return_value={
                    "last_seen_unix": remote.time.time(),
                    "geo_blocked": False,
                },
            ),
            patch.object(remote, "_strict_position_size", return_value=Decimal("0")),
            patch.object(remote, "_strict_open_orders_for_asset", return_value=[object()]),
        ):
            allowed = remote._authorize_order_for_handoff(rec)

        self.assertFalse(allowed)
        self.assertEqual(rec["status"], "FAILED")
        self.assertIn("open order", rec["error"])


class ExecutorLeaseTests(unittest.TestCase):
    def test_live_executor_lease_exceeds_single_buy_fill_window(self):
        self.assertGreaterEqual(remote.LEASE_SECONDS, 30)
        self.assertGreater(remote.LEASE_SECONDS, executor.FILL_WAIT_SECONDS)

    def test_slack_live_lease_exceeds_resting_limit_window(self):
        rec = {"action": "BUY", "payload": {"limit_order_ttl_seconds": 120}}
        self.assertGreater(remote._lease_seconds_for(rec), 120)


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


    def test_initial_geo_timeout_keeps_queue_worker_available(self):
        with patch.object(executor, "_geo", side_effect=executor.httpx.ConnectTimeout("timed out")):
            geo, status = executor._initial_geo_check()
        self.assertIsNone(geo)
        self.assertIn("Geoblock check unavailable", status)
        self.assertIn("timed out", status)

    def test_initial_explicit_geoblock_still_fails_closed(self):
        with (
            patch.object(executor, "_geo", side_effect=RuntimeError("Polymarket geoblock reports this phone network is blocked")),
            self.assertRaises(RuntimeError),
        ):
            executor._initial_geo_check()

    def test_combo_preview_is_quote_only_geo_tolerant(self):
        payload = {
            "budget_usdc": "1",
            "authorized_max_auto_trade_usdc": "25",
            "max_price": "0.65",
            "leg_position_ids": ["1", "2"],
        }
        summary = {"ok": True, "blended_price": "0.40"}
        with patch.object(executor, "_combo_quote", return_value=(object(), summary)) as quote:
            result = executor._combo_preview(payload, "private", "wallet")
        quote.assert_called_once_with(payload, "private", "wallet", require_geo=False)
        self.assertTrue(result["no_order_placed"])
        self.assertEqual(result["status"], "QUOTE_ONLY")

    def test_combo_buy_requires_strict_geo_quote_path(self):
        payload = {
            "budget_usdc": "1",
            "authorized_max_auto_trade_usdc": "25",
            "max_price": "0.65",
            "leg_position_ids": ["1", "2"],
        }
        fake_quote = object()
        summary = {"ok": True, "rfq_id": "rfq"}
        with (
            patch.object(executor, "_combo_quote", return_value=(fake_quote, summary)) as quote,
            patch.object(executor, "_secure_combo") as secure_combo,
        ):
            client = secure_combo.return_value.__enter__.return_value
            client.accept_combo_quote.return_value = type("Accept", (), {"status": "failed", "reason": "stop"})()
            with self.assertRaises(RuntimeError):
                executor._combo_buy(payload, "private", "wallet")
        quote.assert_called_once_with(payload, "private", "wallet", require_geo=True)

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
