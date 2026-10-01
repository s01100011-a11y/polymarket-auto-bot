import unittest
from decimal import Decimal
from unittest.mock import patch

from fastapi import HTTPException

from app import dashboard_live_control_v4 as live_control
from app import termux_executor_dashboard as remote


class ExecutorQueueSafetyTests(unittest.TestCase):
    def test_buy_result_requires_positive_fill_even_if_outer_body_says_ok(self):
        self.assertFalse(
            remote._effective_executor_result_ok(
                "BUY",
                True,
                {"ok": False, "filled_shares": "0", "status": "TEST_BUY_UNFILLED_CANCELED"},
            )
        )
        self.assertFalse(
            remote._effective_executor_result_ok(
                "BUY",
                True,
                {"filled_shares": "0"},
            )
        )
        self.assertTrue(
            remote._effective_executor_result_ok(
                "BUY",
                True,
                {"filled_shares": "1.25"},
            )
        )
        self.assertTrue(remote._effective_executor_result_ok("PREVIEW", True, {}))


    def test_min_decimal_odds_gate_requires_approval_below_170(self):
        below = {"max_price": "0.59"}
        at_or_above = {"max_price": "0.588235"}

        self.assertTrue(remote._buy_requires_min_odds_approval(below))
        self.assertEqual(below["approval_reason"], "MIN_ODDS")
        self.assertEqual(below["minimum_decimal_odds"], "1.70")
        self.assertFalse(remote._buy_requires_min_odds_approval(at_or_above))

    def test_min_odds_helper_records_implied_decimal_odds(self):
        payload = {"signal_buy_price": "0.60", "max_price": "0.61"}
        self.assertTrue(remote._buy_requires_min_odds_approval(payload))
        self.assertEqual(payload["signal_decimal_odds"], "1.6667")

    def test_successful_single_buy_recording_does_not_reference_combo_pending_state(self):
        queue_rec = {
            "id": "exec-buy",
            "action": "BUY",
            "payload": {
                "trade_id": "trade-buy",
                "budget_usdc": "10",
                "max_price": "0.50",
                "market_url": "https://polymarket.com/sports/nfl/test-event",
                "market_type": "moneyline",
                "outcome": "Test Team",
            },
        }
        result = {
            "ok": True,
            "filled_shares": "20",
            "entry_price": "0.50",
            "position_before": "0",
            "market": "Test market",
            "outcome": "Test Team",
            "asset_id": "asset-1",
        }
        with (
            patch.object(remote.core, "_load", return_value={}),
            patch.object(remote.core, "_save") as save,
        ):
            remote._record_buy_result(queue_rec, result)

        saved = save.call_args.args[1]
        self.assertIn("trade-buy", saved)
        self.assertEqual(saved["trade-buy"]["status"], "ORDER_SUBMITTED")
        self.assertNotIn("execution_pending", saved["trade-buy"])

    def test_executor_submission_receipt_persists_bot_order_id(self):
        store = {
            "exec-test": {
                "id": "exec-test",
                "action": "BUY",
                "status": "LEASED",
                "payload": {
                    "source": "slack_live",
                    "slack_event_id": "Ev-test",
                    "trade_id": "slack-live-Ev-test",
                },
            }
        }
        body = remote.ExecutorSubmission(
            order_id="0xbot-order",
            trade_id="slack-live-Ev-test",
            asset_id="asset-1",
            accepted_at_unix=123.0,
        )
        with (
            patch.object(remote, "_queue_load", side_effect=lambda: store),
            patch.object(remote, "_queue_save", side_effect=lambda data: store.update(data)),
        ):
            result = remote.executor_submission("exec-test", body, {})

        self.assertTrue(result["ok"])
        self.assertEqual(store["exec-test"]["submission"]["order_id"], "0xbot-order")
        self.assertEqual(store["exec-test"]["submission"]["asset_id"], "asset-1")

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

    def test_remote_total_buy_is_allowed_and_preserved_in_payload(self):
        req = remote.live_trading.LiveTestBuy(
            market_url="https://polymarket.com/sports/nfl/test-event",
            outcome="No",
            market_type="total",
            max_price=Decimal("0.52"),
            budget_usdc=Decimal("20"),
        )
        with (
            patch.object(remote.core, "MAX_AUTO_TRADE_USDC", Decimal("50")),
            patch.object(remote.core, "MAX_PRICE", Decimal("0.95")),
            patch.object(remote.core, "_sports_event_slug", return_value="test-event"),
        ):
            remote._validate_remote_buy(req)
            payload = remote._buy_payload(req, trade_id="test-total")

        self.assertEqual(payload["market_type"], "total")
        self.assertEqual(payload["max_price"], "0.52")
        self.assertEqual(payload["budget_usdc"], "20")

    def test_remote_spread_buy_is_allowed_and_preserved_in_payload(self):
        req = remote.live_trading.LiveTestBuy(
            market_url="https://polymarket.com/sports/nfl/test-event",
            outcome="Atlanta Falcons +3.5",
            market_type="spread",
            max_price=Decimal("0.55"),
            budget_usdc=Decimal("10"),
        )
        with (
            patch.object(remote.core, "MAX_AUTO_TRADE_USDC", Decimal("50")),
            patch.object(remote.core, "MAX_PRICE", Decimal("0.95")),
            patch.object(remote.core, "_sports_event_slug", return_value="test-event"),
        ):
            remote._validate_remote_buy(req)
            payload = remote._buy_payload(req)

        self.assertEqual(payload["market_type"], "spread")

    def test_remote_buy_rejects_unsupported_sports_market_type(self):
        req = remote.live_trading.LiveTestBuy(
            market_url="https://polymarket.com/sports/nfl/test-event",
            outcome="Yes",
            market_type="player-prop",
            max_price=Decimal("0.52"),
            budget_usdc=Decimal("10"),
        )
        with self.assertRaisesRegex(HTTPException, "moneyline, spread, or total"):
            remote._validate_remote_buy(req)

    def test_manual_order_ui_supports_three_market_types(self):
        html = remote.dashboard.DASHBOARD_HTML
        self.assertIn('id="ltMarketType"', html)
        self.assertIn('<option value="moneyline">Moneyline</option>', html)
        self.assertIn('<option value="spread">Spread</option>', html)
        self.assertIn('<option value="total">Total</option>', html)

    def test_remote_ui_uses_dashboard_cap_for_manual_amount(self):
        html = remote.dashboard.DASHBOARD_HTML
        self.assertIn("liveManualMaxUsdc=Number(d.remote_max_usdc)", html)
        self.assertIn("budget.max=String(liveManualMaxUsdc)", html)

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
