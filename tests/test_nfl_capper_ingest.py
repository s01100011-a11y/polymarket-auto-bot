from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app import nfl_capper_ingest as capper


def _pick(**overrides):
    row = {
        "source": "SLAM - All Access",
        "posted_at": "2026-09-24T04:00:00+00:00",
        "selection": "RAMS -6.5",
        "teams": ["LAR"],
        "bet_types": ["spread"],
        "period": None,
        "spread_lines": ["-6.5"],
        "total_side": None,
        "total_line": None,
        "units": None,
        "status": "active",
    }
    row.update(overrides)
    return row


class NflCapperSourceTests(unittest.TestCase):
    def test_exact_display_names_still_work(self):
        self.assertEqual(capper._source_label(_pick(source="SLAM - All Access")), "Slam - NFL")
        self.assertEqual(capper._source_label(_pick(source="The Syndicate")), "Syndicate - NFL")

    def test_slam_username_is_recognized(self):
        self.assertEqual(capper._source_label(_pick(source="slamthebookie")), "Slam - NFL")
        self.assertEqual(capper._source_label(_pick(source="@slam_the_bookie")), "Slam - NFL")

    def test_syndicate_username_variant_is_recognized(self):
        self.assertEqual(capper._source_label(_pick(source="the_syndicate_vip")), "Syndicate - NFL")

    def test_bridge_source_key_is_preferred_identity(self):
        self.assertEqual(
            capper._source_label(_pick(source="some_mutable_title", source_key="slam")),
            "Slam - NFL",
        )
        self.assertEqual(
            capper._source_label(_pick(source="another_title", source_key="syndicate")),
            "Syndicate - NFL",
        )

    def test_unknown_source_remains_untracked(self):
        self.assertIsNone(capper._source_label(_pick(source="Blacksmith Bets Standard VIP")))


class NflCapperFeedLoggingTests(unittest.TestCase):
    def test_feed_signature_ignores_generated_timestamp(self):
        left = {
            "generated_at": "2026-09-25T00:00:00+00:00",
            "scanned_posts": 1,
            "detected_posts": 1,
            "detected_picks": 1,
            "listener_connected": True,
            "listener_ready": True,
            "picks": [{"source": "SLAM - All Access", "selection": "ATL +3"}],
            "unparsed_recent": [],
        }
        right = dict(left)
        right["generated_at"] = "2026-09-25T00:00:15+00:00"
        self.assertEqual(capper._feed_content_signature(left), capper._feed_content_signature(right))

    def test_feed_signature_changes_when_pick_content_changes(self):
        left = {
            "scanned_posts": 1,
            "detected_posts": 1,
            "detected_picks": 1,
            "listener_connected": True,
            "listener_ready": True,
            "picks": [{"source": "SLAM - All Access", "selection": "ATL +3"}],
            "unparsed_recent": [],
        }
        right = dict(left)
        right["picks"] = [{"source": "SLAM - All Access", "selection": "ATL +3.5"}]
        self.assertNotEqual(capper._feed_content_signature(left), capper._feed_content_signature(right))


class NflCapperSizingTests(unittest.TestCase):
    def test_default_and_sub_one_unit_both_buy_one_unit(self):
        self.assertEqual(str(capper._stake_for_pick(_pick(units=None))), "10.00")
        self.assertEqual(str(capper._stake_for_pick(_pick(units=0.5))), "10.00")
        self.assertEqual(str(capper._stake_for_pick(_pick(units=1))), "10.00")

    def test_explicit_units_scale_at_ten_dollars(self):
        self.assertEqual(str(capper._stake_for_pick(_pick(units=1.25))), "12.50")
        self.assertEqual(str(capper._stake_for_pick(_pick(units=3))), "30.00")

    def test_props_and_period_markets_are_rejected(self):
        kind, reason = capper._classify_pick(_pick(bet_types=["prop"]))
        self.assertIsNone(kind)
        self.assertIn("props", reason)
        kind, reason = capper._classify_pick(_pick(period="2H", bet_types=["spread", "period"]))
        self.assertIsNone(kind)
        self.assertIn("period", reason)

    def test_ambiguous_commentary_is_rejected(self):
        kind, reason = capper._classify_pick(
            _pick(selection="If the Rams had won last week this would be -9.5 and the book liability here is poor so let us up Rams to max bet")
        )
        self.assertIsNone(kind)
        self.assertIn("ambiguous", reason)

    def test_one_team_total_is_supported_for_exact_resolution(self):
        kind, reason = capper._classify_pick(
            _pick(
                selection="UNDER 43.5",
                teams=["ATL"],
                bet_types=["total"],
                spread_lines=[],
                total_side="UNDER",
                total_line=43.5,
            )
        )
        self.assertEqual(kind, "total")
        self.assertIsNone(reason)

    def test_zero_team_total_is_rejected(self):
        kind, reason = capper._classify_pick(
            _pick(
                selection="UNDER 43.5",
                teams=[],
                bet_types=["total"],
                spread_lines=[],
                total_side="UNDER",
                total_line=43.5,
            )
        )
        self.assertIsNone(kind)
        self.assertIn("at least one", reason)

    def test_fingerprint_changes_when_units_change(self):
        self.assertNotEqual(capper._fingerprint(_pick(units=1)), capper._fingerprint(_pick(units=2)))


class NflCapperMarketResolutionTests(unittest.TestCase):
    @staticmethod
    def _event(slug: str, title: str, line: float = 43.5):
        yes = SimpleNamespace(label="Yes", token_id=f"{slug}-yes")
        no = SimpleNamespace(label="No", token_id=f"{slug}-no")
        market = SimpleNamespace(
            id=f"{slug}-market",
            question=f"Will {title} be Over {line}?",
            slug=f"{slug}-total-{line}",
            sports=SimpleNamespace(sports_market_type="total"),
            outcomes=SimpleNamespace(yes=yes, no=no),
            state=SimpleNamespace(accepting_orders=True),
        )
        return SimpleNamespace(id=slug, slug=slug, title=title, markets=[market])

    @staticmethod
    def _client(events):
        class _Result:
            def __init__(self, items):
                self.items = items

            def first_page(self):
                return self

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def list_events(self, **kwargs):
                return _Result(events)

        return _Client()

    def test_one_team_total_resolves_when_exactly_one_event_matches(self):
        event = self._event("atl-was", "Atlanta Falcons vs Washington Commanders")
        pick = _pick(
            selection="UNDER 43.5",
            teams=["ATL"],
            bet_types=["total"],
            spread_lines=[],
            total_side="UNDER",
            total_line=43.5,
        )
        with patch.object(capper, "PublicClient", return_value=self._client([event])):
            matched_event, _, label, outcome = capper._find_market(pick, "total")
        self.assertEqual(matched_event.slug, "atl-was")
        self.assertEqual(label, "No")
        self.assertEqual(outcome.token_id, "atl-was-no")

    def test_one_team_total_blocks_when_two_events_match_exact_line(self):
        events = [
            self._event("atl-was", "Atlanta Falcons vs Washington Commanders"),
            self._event("atl-car", "Atlanta Falcons vs Carolina Panthers"),
        ]
        pick = _pick(
            selection="UNDER 43.5",
            teams=["ATL"],
            bet_types=["total"],
            spread_lines=[],
            total_side="UNDER",
            total_line=43.5,
        )
        with patch.object(capper, "PublicClient", return_value=self._client(events)):
            with self.assertRaisesRegex(ValueError, "exactly one event"):
                capper._find_market(pick, "total")


class NflCapperOutcomeTests(unittest.TestCase):
    @staticmethod
    def _binary_market(question: str):
        yes = SimpleNamespace(label="Yes", token_id="yes-token")
        no = SimpleNamespace(label="No", token_id="no-token")
        sports = SimpleNamespace(sports_market_type="total")
        return SimpleNamespace(
            question=question,
            slug="test-total",
            sports=sports,
            outcomes=SimpleNamespace(yes=yes, no=no),
        )

    def test_under_maps_to_no_when_question_is_over(self):
        market = self._binary_market("Will Rams vs Giants be Over 45.5?")
        pick = _pick(
            selection="RAMS/GIANTS UNDER 45.5",
            teams=["LAR", "NYG"],
            bet_types=["total"],
            spread_lines=[],
            total_side="UNDER",
            total_line=45.5,
        )
        label, outcome = capper._select_outcome(market, pick, "total")
        self.assertEqual(label, "No")
        self.assertEqual(outcome.token_id, "no-token")

    def test_over_maps_to_yes_when_question_is_over(self):
        market = self._binary_market("Will Rams vs Giants be Over 45.5?")
        pick = _pick(
            selection="RAMS/GIANTS OVER 45.5",
            teams=["LAR", "NYG"],
            bet_types=["total"],
            spread_lines=[],
            total_side="OVER",
            total_line=45.5,
        )
        label, outcome = capper._select_outcome(market, pick, "total")
        self.assertEqual(label, "Yes")
        self.assertEqual(outcome.token_id, "yes-token")


class NflCapperPreviewTests(unittest.TestCase):
    def test_test_preview_queues_preview_never_buy(self):
        pick = _pick(
            selection="UNDER 48.5",
            teams=["ATL", "GB"],
            bet_types=["total"],
            spread_lines=[],
            total_side="UNDER",
            total_line=48.5,
            units=2,
        )
        event = SimpleNamespace(slug="atl-gb-2026-09-25", title="Falcons vs Packers")
        market = SimpleNamespace(question="Will Falcons vs Packers be Over 48.5?")
        outcome = SimpleNamespace(token_id="under-48-5-token")

        class _Book:
            asks = [SimpleNamespace(price="0.64")]

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get_price(self, **kwargs):
                return "0.63"

            def get_spread(self, **kwargs):
                return "0.02"

            def get_order_book(self, **kwargs):
                return _Book()

        core = SimpleNamespace(
            MAX_AUTO_TRADE_USDC=Decimal("50"),
            MAX_PRICE=Decimal("0.95"),
            MAX_SPREAD=Decimal("0.08"),
            MAX_DAILY_BUDGET_USDC=Decimal("100"),
            auto_trading_enabled=lambda: True,
            _daily_budget_used=lambda: Decimal("0"),
        )

        calls = []

        class _Remote:
            def _queue_load(self):
                return {}

            def _enqueue(self, action, payload):
                calls.append((action, payload))
                return {"id": "exec-preview-test"}

        with (
            patch.object(capper.live_control, "_executor_ready", return_value=(True, {})),
            patch.object(capper, "_find_market", return_value=(event, market, "No", outcome)),
            patch.object(capper, "PublicClient", return_value=_Client()),
        ):
            result = capper._prepare_test_preview(
                pick,
                core=core,
                remote=_Remote(),
                unit_usdc=Decimal("10"),
            )

        self.assertEqual(result["status"], "PREVIEW_QUEUED")
        self.assertEqual(result["stake_usdc"], "20.00")
        self.assertEqual(result["max_price"], "0.64")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "PREVIEW")
        self.assertEqual(calls[0][1]["market_type"], "total")
        self.assertEqual(calls[0][1]["budget_usdc"], "20.00")
        self.assertEqual(calls[0][1]["asset_id"], "under-48-5-token")
        self.assertFalse(calls[0][1]["auto"])


class NflCapperStatsTests(unittest.TestCase):
    def test_stats_are_separated_by_capper_and_pushes(self):
        executions = {
            "slam-win": {
                "strategy_source": "Slam - NFL",
                "strategy_units": "2",
                "status": "SETTLED_WIN",
                "budget_usdc": "20",
                "realized_pnl": "15",
                "settlement": {"result": "WIN"},
            },
            "slam-open": {
                "strategy_source": "Slam - NFL",
                "strategy_units": "1",
                "status": "ORDER_SUBMITTED",
                "budget_usdc": "10",
            },
            "syn-loss": {
                "strategy_source": "Syndicate - NFL",
                "strategy_units": "1.25",
                "status": "SETTLED_LOSS",
                "budget_usdc": "12.50",
                "realized_pnl": "-12.50",
                "settlement": {"result": "LOSS"},
            },
            "syn-push": {
                "strategy_source": "Syndicate - NFL",
                "strategy_units": "1",
                "status": "SETTLED_PUSH",
                "budget_usdc": "10",
                "realized_pnl": "0.25",
                "settlement": {"result": "PUSH"},
            },
        }
        stats = capper._stats_from_executions(executions)
        slam = stats["Slam - NFL"]
        syndicate = stats["Syndicate - NFL"]

        self.assertEqual(slam["bets"], 2)
        self.assertEqual(slam["open"], 1)
        self.assertEqual(slam["wins"], 1)
        self.assertEqual(slam["losses"], 0)
        self.assertEqual(slam["realized_pnl_usdc"], "15.00")
        self.assertEqual(slam["roi_pct"], "75.0")

        self.assertEqual(syndicate["bets"], 2)
        self.assertEqual(syndicate["wins"], 0)
        self.assertEqual(syndicate["losses"], 1)
        self.assertEqual(syndicate["pushes"], 1)
        self.assertEqual(syndicate["units_staked"], "2.25")
        self.assertEqual(syndicate["realized_pnl_usdc"], "-12.25")
        self.assertEqual(syndicate["roi_pct"], "-54.4")


if __name__ == "__main__":
    unittest.main()
