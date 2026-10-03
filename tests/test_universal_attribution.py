from __future__ import annotations

from decimal import Decimal
import unittest

from app import attribution_core as attribution
from app import universal_position_identity as identity


class UniversalPositionIdentityTests(unittest.TestCase):
    def test_opposite_outcome_inverts_named_spread(self):
        rec = {
            "quote": {
                "market_type": "spread",
                "market": "Spread: Virginia Tech (-6.5)",
                "requested_outcome": "Pittsburgh",
                "resolved_outcome": "Pittsburgh",
            }
        }
        self.assertEqual(identity.effective_spread(rec), Decimal("6.5"))
        self.assertEqual(identity.canonical_exact_position(rec), "Pittsburgh +6.5")

    def test_named_outcome_keeps_market_spread_sign(self):
        rec = {
            "quote": {
                "market_type": "spread",
                "market": "Spread: Virginia Tech (-6.5)",
                "requested_outcome": "Virginia Tech",
            }
        }
        self.assertEqual(identity.effective_spread(rec), Decimal("-6.5"))
        self.assertEqual(identity.canonical_exact_position(rec), "Virginia Tech -6.5")


class AttributionTests(unittest.TestCase):
    def test_large_live_alternate_does_not_count_for_original_capper(self):
        rec = {
            "strategy_source": "Slam - CFB",
            "strategy_pick_id": "pick-1",
            "strategy_selection": "PITT +3",
            "strategy_requested_spread_line": "+3",
            "quote": {
                "market_type": "spread",
                "market": "Spread: Virginia Tech (-6.5)",
                "requested_outcome": "Pittsburgh",
                "resolved_outcome": "Pittsburgh",
            },
        }
        source, reason = attribution.attribution_for_execution(rec)
        self.assertEqual(source, "SH01")
        self.assertIn("spread_changed", reason)

    def test_exact_original_spread_keeps_capper_credit(self):
        rec = {
            "strategy_source": "Slam - CFB",
            "strategy_pick_id": "pick-2",
            "strategy_selection": "PITT +6.5",
            "strategy_requested_spread_line": "+6.5",
            "quote": {
                "market_type": "spread",
                "market": "Spread: Virginia Tech (-6.5)",
                "requested_outcome": "Pittsburgh",
                "resolved_outcome": "Pittsburgh",
            },
        }
        self.assertEqual(attribution.attribution_for_execution(rec)[0], "Slam - CFB")

    def test_spread_exactly_one_point_variance_keeps_capper_credit(self):
        rec = {
            "strategy_source": "Slam - CFB",
            "strategy_pick_id": "pick-spread-1pt",
            "strategy_selection": "PITT +3",
            "strategy_requested_spread_line": "+3",
            "quote": {
                "market_type": "spread",
                "market": "Spread: Virginia Tech (-4)",
                "requested_outcome": "Pittsburgh",
                "resolved_outcome": "Pittsburgh",
            },
        }
        source, reason = attribution.attribution_for_execution(rec)
        self.assertEqual(source, "Slam - CFB")
        self.assertIn("within_1pt", reason)

    def test_spread_more_than_one_point_variance_moves_to_sh01(self):
        rec = {
            "strategy_source": "Slam - CFB",
            "strategy_pick_id": "pick-spread-15pt",
            "strategy_selection": "PITT +3",
            "strategy_requested_spread_line": "+3",
            "quote": {
                "market_type": "spread",
                "market": "Spread: Virginia Tech (-4.5)",
                "requested_outcome": "Pittsburgh",
                "resolved_outcome": "Pittsburgh",
            },
        }
        self.assertEqual(attribution.attribution_for_execution(rec)[0], "SH01")

    def test_total_exactly_one_point_variance_keeps_capper_credit(self):
        rec = {
            "strategy_source": "Slam - CFB",
            "strategy_pick_id": "pick-total-1pt",
            "strategy_selection": "PENN STATE/NORTHWESTERN UNDER 46.5",
            "strategy_requested_total_line": "46.5",
            "strategy_executed_total_line": "47.5",
            "quote": {
                "market_type": "total",
                "requested_outcome": "UNDER",
                "resolved_outcome": "UNDER",
            },
        }
        source, reason = attribution.attribution_for_execution(rec)
        self.assertEqual(source, "Slam - CFB")
        self.assertIn("within_1pt", reason)

    def test_changed_total_over_one_point_moves_to_sh01(self):
        rec = {
            "strategy_source": "Slam - CFB",
            "strategy_pick_id": "pick-3",
            "strategy_selection": "PENN STATE/NORTHWESTERN UNDER 46.5",
            "strategy_requested_total_line": "46.5",
            "strategy_executed_total_line": "48",
            "quote": {
                "market_type": "total",
                "requested_outcome": "UNDER",
                "resolved_outcome": "UNDER",
            },
        }
        self.assertEqual(attribution.attribution_for_execution(rec)[0], "SH01")

    def test_total_side_flip_is_sh01_even_with_same_line(self):
        rec = {
            "strategy_source": "Slam - CFB",
            "strategy_pick_id": "pick-side-flip",
            "strategy_selection": "PENN STATE/NORTHWESTERN UNDER 46.5",
            "strategy_requested_total_line": "46.5",
            "strategy_executed_total_line": "46.5",
            "quote": {
                "market_type": "total",
                "requested_outcome": "UNDER",
                "resolved_outcome": "OVER",
            },
        }
        self.assertEqual(attribution.attribution_for_execution(rec)[0], "SH01")

    def test_unlinked_dashboard_trade_is_sh01(self):
        rec = {
            "strategy_source": "Slam - NFL",
            "quote": {"market_type": "moneyline", "requested_outcome": "Chiefs"},
        }
        self.assertEqual(attribution.attribution_for_execution(rec)[0], "SH01")


class UnitValueTests(unittest.TestCase):
    def test_persisted_unit_value_wins(self):
        self.assertEqual(attribution.unit_value_usdc({"strategy_unit_usdc": "20"}), Decimal("20"))

    def test_unit_value_recovers_from_target_profit(self):
        rec = {"strategy_units": "2", "strategy_target_profit_usdc": "40"}
        self.assertEqual(attribution.unit_value_usdc(rec), Decimal("20"))

    def test_to_win_unit_value_recovers_from_entry_economics(self):
        rec = {
            "strategy_units": "2",
            "strategy_sizing_mode": "TO_WIN",
            "actual_cost_usdc": "60",
            "quote": {"entry_price": "0.60"},
        }
        # $60 risk at 0.60 returns $40 profit. A 2u call therefore implies $20/u.
        self.assertEqual(attribution.unit_value_usdc(rec), Decimal("20"))


if __name__ == "__main__":
    unittest.main()
