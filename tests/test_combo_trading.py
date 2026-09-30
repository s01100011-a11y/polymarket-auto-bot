from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app import combo_trading
from app import main as core
from app import termux_executor_dashboard as remote
from scripts import termux_executor as executor


class _Client:
    def __init__(self, market):
        self.market = market

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _market(
    position_id: str,
    market_type: str = "moneyline",
    *,
    yes_label: str = "Yes",
    no_label: str = "No",
    group_item_title: str | None = None,
    line=None,
    question: str = "Test market",
):
    yes = SimpleNamespace(label=yes_label, position_id=position_id, token_id=position_id + "-token")
    no = SimpleNamespace(label=no_label, position_id=position_id + "-no", token_id=position_id + "-no-token")
    return SimpleNamespace(
        question=question,
        slug=position_id + "-market",
        group_item_title=group_item_title,
        outcomes=SimpleNamespace(yes=yes, no=no),
        sports=SimpleNamespace(sports_market_type=market_type, line=line),
        state=SimpleNamespace(accepting_orders=True, active=True),
    )


class ComboTradingTests(unittest.TestCase):
    def test_combo_request_requires_two_legs(self):
        with self.assertRaises(Exception):
            combo_trading.ComboRequest(
                legs=[
                    combo_trading.ComboLeg(
                        market_url="https://polymarket.com/sports/nfl/test",
                        outcome="Yes",
                        market_type="moneyline",
                    )
                ],
                budget_usdc=Decimal("10"),
            )

    def test_resolve_combo_legs_uses_position_ids_and_rejects_duplicates(self):
        market = _market("pos-1")
        legs = [
            combo_trading.ComboLeg(
                market_url="https://polymarket.com/sports/nfl/a",
                outcome="Yes",
                market_type="moneyline",
            ),
            combo_trading.ComboLeg(
                market_url="https://polymarket.com/sports/nfl/b",
                outcome="Yes",
                market_type="moneyline",
            ),
        ]
        with (
            patch.object(combo_trading, "PublicClient", return_value=_Client(market)),
            patch.object(core, "_select_market", return_value=market),
            patch.object(core, "_sports_event_slug", return_value="event"),
        ):
            with self.assertRaisesRegex(HTTPException, "duplicate"):
                combo_trading.resolve_combo_legs(core, legs)

    def test_grouped_soccer_moneyline_selects_named_binary_condition(self):
        seychelles = _market(
            "sey",
            "moneyline",
            group_item_title="Seychelles",
            question="Will Seychelles win?",
        )
        draw = _market(
            "draw",
            "moneyline",
            group_item_title="Draw",
            question="Will Seychelles vs. Sri Lanka end in a draw?",
        )
        sri_lanka = _market(
            "sri",
            "moneyline",
            group_item_title="Sri Lanka",
            question="Will Sri Lanka win?",
        )
        event = SimpleNamespace(
            title="Seychelles vs. Sri Lanka",
            markets=[seychelles, draw, sri_lanka],
        )

        class _EventClient:
            def get_event(self, **kwargs):
                return event

        intent = core.TradeIntent(
            market_url="https://polymarket.com/sports/fifa-friendlies/fif-sey-sri-2026-09-30",
            outcome="Sri Lanka",
            market_type="moneyline",
            max_price=Decimal("0.95"),
            budget_usdc=Decimal("1"),
        )
        selected = core._select_market(_EventClient(), intent)
        self.assertIs(selected, sri_lanka)
        position_id, label = combo_trading._resolve_position_id(core, selected, "Sri Lanka")
        self.assertEqual(position_id, "sri")
        self.assertEqual(label, "Sri Lanka")

    def test_total_plural_type_and_line_select_under(self):
        total_25 = _market(
            "tot25-over",
            "totals",
            yes_label="Over",
            no_label="Under",
            line=2.5,
            question="Seychelles vs. Sri Lanka: O/U 2.5",
        )
        total_35 = _market(
            "tot35-over",
            "totals",
            yes_label="Over",
            no_label="Under",
            line=3.5,
            question="Seychelles vs. Sri Lanka: O/U 3.5",
        )
        event = SimpleNamespace(
            title="Seychelles vs. Sri Lanka",
            markets=[total_35, total_25],
        )

        class _EventClient:
            def get_event(self, **kwargs):
                return event

        intent = core.TradeIntent(
            market_url="https://polymarket.com/sports/fifa-friendlies/fif-sey-sri-2026-09-30",
            outcome="Under 2.5",
            market_type="total",
            max_price=Decimal("0.95"),
            budget_usdc=Decimal("1"),
        )
        selected = core._select_market(_EventClient(), intent)
        self.assertIs(selected, total_25)
        position_id, label = combo_trading._resolve_position_id(core, selected, "Under 2.5")
        self.assertEqual(position_id, "tot25-over-no")
        self.assertEqual(label, "Under 2.5")

    def test_spread_plural_type_uses_complementary_line_for_no_side(self):
        spread = _market(
            "ind-215",
            "spreads",
            yes_label="Indiana",
            no_label="Northwestern",
            line=-21.5,
            question="Spread: Indiana (-21.5)",
        )
        event = SimpleNamespace(title="Northwestern vs. Indiana", markets=[spread])

        class _EventClient:
            def get_event(self, **kwargs):
                return event

        intent = core.TradeIntent(
            market_url="https://polymarket.com/sports/cfb/cfb-nw-ind-2026-09-25",
            outcome="Northwestern +21.5",
            market_type="spread",
            max_price=Decimal("0.95"),
            budget_usdc=Decimal("1"),
        )
        selected = core._select_market(_EventClient(), intent)
        self.assertIs(selected, spread)
        position_id, label = combo_trading._resolve_position_id(core, selected, "Northwestern +21.5")
        self.assertEqual(position_id, "ind-215-no")
        self.assertIn("Northwestern", label)

        bad = intent.model_copy(update={"outcome": "Northwestern +20.5"})
        with self.assertRaises(HTTPException):
            core._select_market(_EventClient(), bad)

    def test_combo_buy_is_subject_to_executor_fill_rule(self):
        self.assertFalse(
            remote._effective_executor_result_ok(
                "COMBO_BUY",
                True,
                {"ok": True, "filled_shares": "0"},
            )
        )
        self.assertTrue(
            remote._effective_executor_result_ok(
                "COMBO_BUY",
                True,
                {"ok": True, "filled_shares": "12.5"},
            )
        )

    def test_combo_accepted_pending_is_not_treated_as_unfilled_failure(self):
        self.assertTrue(
            remote._effective_executor_result_ok(
                "COMBO_BUY",
                True,
                {
                    "ok": True,
                    "accepted": True,
                    "status": "COMBO_EXECUTION_PENDING",
                    "filled_shares": "0",
                },
            )
        )

    def test_combo_buy_expires_like_single_buy(self):
        data = {
            "exec-combo": {
                "id": "exec-combo",
                "action": "COMBO_BUY",
                "status": "PENDING",
                "created_unix": 100.0,
            }
        }
        with patch.object(remote, "EXECUTOR_BUY_TTL_SECONDS", 180):
            expired = remote._expire_stale_buys(data, now=281.0)
        self.assertEqual(expired, ["exec-combo"])
        self.assertEqual(data["exec-combo"]["status"], "FAILED")

    def test_combo_handoff_stamps_current_dashboard_cap(self):
        rec = {
            "id": "exec-combo",
            "action": "COMBO_BUY",
            "status": "PENDING",
            "payload": {"budget_usdc": "25"},
        }
        with (
            patch.object(remote.core, "MAX_AUTO_TRADE_USDC", Decimal("50")),
            patch.object(remote.core, "bot_enabled", return_value=True),
            patch.object(remote.core, "live_trading_enabled", return_value=True),
            patch.object(remote.core, "auto_trading_enabled", return_value=True),
            patch.dict(remote.os.environ, {"COMBO_TRADING_ENABLED": "true"}, clear=False),
        ):
            allowed = remote._authorize_order_for_handoff(rec)
        self.assertTrue(allowed)
        self.assertEqual(rec["payload"]["authorized_max_auto_trade_usdc"], "50")

    def test_combo_handoff_blocks_when_combo_gate_is_off(self):
        rec = {
            "id": "exec-combo",
            "action": "COMBO_BUY",
            "status": "PENDING",
            "payload": {"budget_usdc": "10"},
        }
        with (
            patch.object(remote.core, "bot_enabled", return_value=True),
            patch.object(remote.core, "live_trading_enabled", return_value=True),
            patch.object(remote.core, "auto_trading_enabled", return_value=True),
            patch.dict(remote.os.environ, {"COMBO_TRADING_ENABLED": "false"}, clear=False),
        ):
            allowed = remote._authorize_order_for_handoff(rec)
        self.assertFalse(allowed)
        self.assertEqual(rec["status"], "FAILED")
        self.assertIn("gate is OFF", rec["error"])

    def test_termux_combo_payload_validates_duplicates_and_max_price(self):
        payload = {
            "budget_usdc": "20",
            "authorized_max_auto_trade_usdc": "50",
            "leg_position_ids": ["1", "2"],
            "max_price": "0.60",
        }
        ids, budget, max_price = executor._validate_combo_payload(payload)
        self.assertEqual(ids, ["1", "2"])
        self.assertEqual(budget, Decimal("20"))
        self.assertEqual(max_price, Decimal("0.60"))

        payload["leg_position_ids"] = ["1", "1"]
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            executor._validate_combo_payload(payload)

    def test_builder_key_env_requires_complete_credentials(self):
        with (
            patch.dict(
                executor.os.environ,
                {
                    "POLYMARKET_BUILDER_API_KEY": "key",
                    "POLYMARKET_BUILDER_SECRET": "",
                    "POLYMARKET_BUILDER_PASSPHRASE": "",
                },
                clear=False,
            ),
            self.assertRaisesRegex(RuntimeError, "incomplete"),
        ):
            executor._builder_key_from_env()

    def test_smoke_token_requires_configured_match_and_live_combo_off(self):
        with (
            patch.dict(combo_trading.os.environ, {"COMBO_SMOKE_TOKEN": "abc123", "COMBO_TRADING_ENABLED": "false"}, clear=False),
        ):
            combo_trading._require_smoke_token("abc123")

        with (
            patch.dict(combo_trading.os.environ, {"COMBO_SMOKE_TOKEN": "abc123", "COMBO_TRADING_ENABLED": "false"}, clear=False),
            self.assertRaises(HTTPException),
        ):
            combo_trading._require_smoke_token("wrong")

        with (
            patch.dict(combo_trading.os.environ, {"COMBO_SMOKE_TOKEN": "abc123", "COMBO_TRADING_ENABLED": "true"}, clear=False),
            self.assertRaisesRegex(HTTPException, "disabled while live Combo trading is enabled"),
        ):
            combo_trading._require_smoke_token("abc123")


if __name__ == "__main__":
    unittest.main()
