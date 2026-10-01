from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
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


class NflCapperPortfolioUnitTests(unittest.TestCase):
    @staticmethod
    def _core():
        storage = {}

        def _load(path):
            return storage.get(str(path), {})

        def _save(path, value):
            storage[str(path)] = value

        return SimpleNamespace(
            DATA_DIR=Path("/tmp/capper-unit-test"),
            _load=_load,
            _save=_save,
        )

    def test_portfolio_mode_defaults_to_ten_percent_and_recalculates(self):
        core = self._core()
        state = {"usdc_balance": "300", "portfolio_value": "200"}
        with patch.object(capper.live_control.remote, "_state", return_value=state):
            config = capper._capper_unit_config(core, "Slam - NFL", Decimal("10"))
            self.assertEqual(config["mode"], "fixed")
            self.assertEqual(config["portfolio_pct"], "10.00")
            self.assertEqual(config["unit_usdc"], "10.00")

            capper._set_capper_portfolio_pct(core, "Slam - NFL", Decimal("10"))
            config = capper._capper_unit_config(core, "Slam - NFL", Decimal("10"))
            self.assertEqual(config["mode"], "portfolio_pct")
            self.assertEqual(config["portfolio_value_usdc"], "500.00")
            self.assertEqual(config["unit_usdc"], "50.00")

            state["usdc_balance"] = "400"
            state["portfolio_value"] = "250"
            config = capper._capper_unit_config(core, "Slam - NFL", Decimal("10"))
            self.assertEqual(config["portfolio_value_usdc"], "650.00")
            self.assertEqual(config["unit_usdc"], "65.00")

    def test_call_unit_snapshot_is_immutable_after_unit_setting_changes(self):
        record = {
            "id": "signal-1",
            "first_seen_at": "2026-09-28T01:00:00+00:00",
            "unit_usdc": "10.00",
            "target_profit_usdc": "10.00",
            "unit_mode": "fixed",
        }
        pick = _pick(units=1, decimal_odds=2.0)
        first_config = {
            "mode": "fixed",
            "unit_usdc": "10.00",
            "portfolio_pct": "10.00",
            "portfolio_value_usdc": "100.00",
        }
        changed = capper._ensure_signal_call_unit_snapshot(
            record,
            pick,
            unit_config=first_config,
            effective_unit_usdc=Decimal("10"),
        )
        self.assertTrue(changed)
        self.assertEqual(record["unit_usdc_at_call"], "10.00")
        self.assertEqual(record["target_profit_usdc_at_call"], "10.00")

        record["unit_usdc"] = "50.00"
        record["target_profit_usdc"] = "50.00"
        record["unit_mode"] = "portfolio_pct"
        second_config = {
            "mode": "portfolio_pct",
            "unit_usdc": "50.00",
            "portfolio_pct": "10.00",
            "portfolio_value_usdc": "500.00",
        }
        changed = capper._ensure_signal_call_unit_snapshot(
            record,
            pick,
            unit_config=second_config,
            effective_unit_usdc=Decimal("50"),
        )
        self.assertFalse(changed)
        self.assertEqual(record["unit_usdc_at_call"], "10.00")
        self.assertEqual(record["target_profit_usdc_at_call"], "10.00")
        self.assertEqual(record["unit_mode_at_call"], "fixed")

    def test_fixed_button_switches_portfolio_mode_off(self):
        core = self._core()
        with patch.object(
            capper.live_control.remote,
            "_state",
            return_value={"usdc_balance": "300", "portfolio_value": "200"},
        ):
            capper._set_capper_portfolio_pct(core, "Slam - NFL", Decimal("10"))
            capper._set_capper_unit_usdc(core, "Slam - NFL", Decimal("25"))
            config = capper._capper_unit_config(core, "Slam - NFL", Decimal("10"))
        self.assertEqual(config["mode"], "fixed")
        self.assertEqual(config["unit_usdc"], "25.00")
        self.assertEqual(config["portfolio_pct"], "10.00")

    def test_portfolio_mode_fails_closed_when_wallet_value_is_unavailable(self):
        core = self._core()
        with patch.object(capper.live_control.remote, "_state", return_value={}):
            capper._set_capper_portfolio_pct(core, "Slam - NFL", Decimal("10"))
            config = capper._capper_unit_config(core, "Slam - NFL", Decimal("10"))
            self.assertEqual(config["mode"], "portfolio_pct")
            self.assertIsNotNone(config["error"])
            with self.assertRaisesRegex(RuntimeError, "Portfolio value is unavailable"):
                capper._capper_unit_usdc(core, "Slam - NFL", Decimal("10"))


class NflCapperSizingTests(unittest.TestCase):
    def test_default_and_sub_one_unit_target_one_unit_profit(self):
        self.assertEqual(str(capper._target_profit_for_pick(_pick(units=None))), "10.00")
        self.assertEqual(str(capper._target_profit_for_pick(_pick(units=0.5))), "10.00")
        self.assertEqual(str(capper._target_profit_for_pick(_pick(units=1))), "10.00")
        # Missing posted odds falls back to -115, so risking $11.50 targets $10 profit.
        self.assertEqual(str(capper._stake_for_pick(_pick(units=1))), "11.50")

    def test_explicit_units_scale_profit_target_not_flat_stake(self):
        self.assertEqual(str(capper._target_profit_for_pick(_pick(units=1.25))), "12.50")
        self.assertEqual(str(capper._target_profit_for_pick(_pick(units=3))), "30.00")
        self.assertEqual(str(capper._stake_for_pick(_pick(units=1.25))), "14.38")
        self.assertEqual(str(capper._stake_for_pick(_pick(units=3))), "34.50")

    def test_binary_price_stake_is_calculated_to_win_target(self):
        self.assertEqual(
            str(capper._stake_to_win_at_price(Decimal("10"), Decimal("0.54"))),
            "11.74",
        )
        self.assertEqual(
            str(capper._stake_to_win_at_price(Decimal("20"), Decimal("0.64"))),
            "35.56",
        )

    def test_explicit_college_matchup_with_nfl_nickname_is_rejected(self):
        kind, reason = capper._classify_pick(
            _pick(
                selection="Hawaii Rainbow Warriors v Wyoming Cowboys Under 45.5 Points",
                teams=["DAL"],
                bet_types=["total"],
                spread_lines=[],
                total_side="UNDER",
                total_line=45.5,
            )
        )
        self.assertIsNone(kind)
        self.assertIn("two NFL teams", reason)

    def test_valid_explicit_nfl_matchup_is_allowed(self):
        kind, reason = capper._classify_pick(
            _pick(
                selection="Jaguars v Patriots Over 44.5 Points",
                teams=["JAX", "NE"],
                bet_types=["total"],
                spread_lines=[],
                total_side="OVER",
                total_line=44.5,
            )
        )
        self.assertEqual(kind, "total")
        self.assertIsNone(reason)

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
        return SimpleNamespace(id=slug, slug=f"nfl-{slug}", title=title, markets=[market])

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

    def test_event_discovery_falls_back_from_full_name_to_nickname(self):
        event = self._event("atl-gb-2026-09-25", "Falcons vs Packers")

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
                query = str(kwargs.get("title_search") or "").casefold()
                if query == "atlanta falcons":
                    return _Result([])
                if query == "falcons":
                    return _Result([event])
                return _Result([])

        pick = _pick(
            selection="UNDER 43.5",
            teams=["ATL", "GB"],
            bet_types=["total"],
            spread_lines=[],
            total_side="UNDER",
            total_line=43.5,
        )

        with patch.object(capper, "PublicClient", return_value=_Client()):
            matched_event, _, label, outcome = capper._find_market(pick, "total")

        self.assertEqual(matched_event.slug, "nfl-atl-gb-2026-09-25")
        self.assertEqual(label, "No")
        self.assertEqual(outcome.token_id, "atl-gb-2026-09-25-no")

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
        self.assertEqual(matched_event.slug, "nfl-atl-was")
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


    def test_total_uses_structured_sports_line_when_text_omits_decimal(self):
        over = SimpleNamespace(label="Over", token_id="atl-gb-over")
        under = SimpleNamespace(label="Under", token_id="atl-gb-under")
        market = SimpleNamespace(
            id="atl-gb-total",
            question="Will Falcons and Packers combine for at least the listed total?",
            slug="nfl-atl-gb-total",
            sports=SimpleNamespace(sports_market_type="total", line=43.5),
            outcomes=SimpleNamespace(yes=over, no=under),
            state=SimpleNamespace(accepting_orders=True),
        )
        event = SimpleNamespace(
            id="atl-gb",
            slug="nfl-atl-gb-2026-09-25",
            title="Atlanta Falcons vs Green Bay Packers",
            markets=[market],
        )
        pick = _pick(
            selection="UNDER 43.5",
            teams=["ATL", "GB"],
            bet_types=["total"],
            spread_lines=[],
            total_side="UNDER",
            total_line=43.5,
        )

        with patch.object(capper, "PublicClient", return_value=self._client([event])):
            matched_event, matched_market, label, outcome = capper._find_market(pick, "total")

        self.assertEqual(matched_event.slug, "nfl-atl-gb-2026-09-25")
        self.assertEqual(matched_market.id, "atl-gb-total")
        self.assertEqual(label, "Under")
        self.assertEqual(outcome.token_id, "atl-gb-under")

    def test_structured_total_line_rejects_different_line(self):
        over = SimpleNamespace(label="Over", token_id="atl-gb-over")
        under = SimpleNamespace(label="Under", token_id="atl-gb-under")
        market = SimpleNamespace(
            id="atl-gb-total",
            question="Will Falcons and Packers combine for at least the listed total?",
            slug="nfl-atl-gb-total",
            sports=SimpleNamespace(sports_market_type="total", line=44.5),
            outcomes=SimpleNamespace(yes=over, no=under),
            state=SimpleNamespace(accepting_orders=True),
        )
        event = SimpleNamespace(
            id="atl-gb",
            slug="nfl-atl-gb-2026-09-25",
            title="Atlanta Falcons vs Green Bay Packers",
            markets=[market],
        )
        pick = _pick(
            selection="UNDER 43.5",
            teams=["ATL", "GB"],
            bet_types=["total"],
            spread_lines=[],
            total_side="UNDER",
            total_line=43.5,
        )

        with patch.object(capper, "PublicClient", return_value=self._client([event])):
            with self.assertRaisesRegex(ValueError, "No exact open Polymarket NFL total market"):
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
        self.assertEqual(result["stake_usdc"], "35.56")
        self.assertEqual(result["max_price"], "0.64")
        self.assertEqual(result["target_profit_usdc"], "20.00")
        self.assertEqual(result["sizing_mode"], "TO_WIN")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "PREVIEW")
        self.assertEqual(calls[0][1]["market_type"], "total")
        self.assertEqual(calls[0][1]["budget_usdc"], "35.56")
        self.assertEqual(calls[0][1]["asset_id"], "under-48-5-token")
        self.assertEqual(calls[0][1]["strategy_target_profit_usdc"], "20.00")
        self.assertEqual(calls[0][1]["strategy_sizing_mode"], "TO_WIN")
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
        live_marks = {
            "slam-open": {
                "id": "slam-open",
                "entry_price": "0.50",
                "current_price": "0.625",
                "shares": "20",
                "estimated_pnl": "2.50",
            }
        }
        stats = capper._stats_from_executions(executions, live_marks=live_marks)
        slam = stats["Slam - NFL"]
        syndicate = stats["Syndicate - NFL"]

        self.assertEqual(slam["bets"], 2)
        self.assertEqual(slam["open"], 1)
        self.assertEqual(slam["wins"], 1)
        self.assertEqual(slam["losses"], 0)
        self.assertEqual(slam["realized_pnl_usdc"], "15.00")
        self.assertEqual(slam["live_pnl_usdc"], "2.50")
        self.assertEqual(slam["total_live_pnl_usdc"], "17.50")
        self.assertEqual(slam["open_cost_basis_usdc"], "10.00")
        self.assertEqual(slam["open_value_usdc"], "12.50")
        self.assertEqual(slam["roi_pct"], "75.0")

        self.assertEqual(syndicate["bets"], 2)
        self.assertEqual(syndicate["wins"], 0)
        self.assertEqual(syndicate["losses"], 1)
        self.assertEqual(syndicate["pushes"], 1)
        self.assertEqual(syndicate["units_staked"], "2.25")
        self.assertEqual(syndicate["realized_pnl_usdc"], "-12.25")
        self.assertEqual(syndicate["total_live_pnl_usdc"], "-12.25")
        self.assertEqual(syndicate["roi_pct"], "-54.4")

    def test_stats_helper_supports_cfb_labels_and_filters_other_sports(self):
        executions = {
            "cfb-win": {
                "strategy_source": "Slam - CFB",
                "strategy_sport": "CFB",
                "strategy_units": "1",
                "status": "SETTLED_WIN",
                "actual_cost_usdc": "10",
                "realized_pnl": "8",
                "settlement": {"result": "WIN"},
            },
            "wrong-sport-loss": {
                "strategy_source": "Slam - CFB",
                "strategy_sport": "NFL",
                "strategy_units": "1",
                "status": "SETTLED_LOSS",
                "actual_cost_usdc": "10",
                "realized_pnl": "-10",
                "settlement": {"result": "LOSS"},
            },
        }

        stats = capper._stats_from_executions(
            executions,
            labels=("Slam - CFB", "Syndicate - CFB"),
            sport="CFB",
        )
        slam = stats["Slam - CFB"]

        self.assertEqual(slam["bets"], 1)
        self.assertEqual(slam["wins"], 1)
        self.assertEqual(slam["losses"], 0)
        self.assertEqual(slam["realized_pnl_usdc"], "8.00")
        self.assertEqual(slam["roi_pct"], "80.0")


class NflMissedPnlTests(unittest.TestCase):
    def test_missed_pnl_counts_only_graded_untraded_calls(self):
        signals = {
            "slam-win": {
                "id": "slam-win",
                "source": "Slam - NFL",
                "stake_usdc": "10",
                "pick_result": "WIN",
                "pick": _pick(decimal_odds=None, american_odds=None),
            },
            "slam-loss": {
                "id": "slam-loss",
                "source": "Slam - NFL",
                "stake_usdc": "10",
                "pick_result": "LOSS",
                "pick": _pick(decimal_odds=None, american_odds=None),
            },
            "slam-traded-win": {
                "id": "slam-traded-win",
                "source": "Slam - NFL",
                "stake_usdc": "10",
                "pick_result": "WIN",
                "pick": _pick(decimal_odds=2.0),
            },
            "slam-unresolved": {
                "id": "slam-unresolved",
                "source": "Slam - NFL",
                "stake_usdc": "10",
                "pick": _pick(decimal_odds=2.0),
            },
            "syndicate-win": {
                "id": "syndicate-win",
                "source": "Syndicate - NFL",
                "stake_usdc": "20",
                "pick_result": "WIN",
                "pick": _pick(source="The Syndicate", units=2, decimal_odds=1.62),
            },
        }
        executions = {
            "real-trade": {
                "id": "real-trade",
                "strategy_pick_id": "slam-traded-win",
                "strategy_source": "Slam - NFL",
                "strategy_sport": "NFL",
                "status": "SETTLED_WIN",
                "paper": False,
            }
        }

        stats = capper._missed_signal_stats(signals, executions)

        self.assertEqual(stats["Slam - NFL"]["missed_graded"], 2)
        self.assertEqual(stats["Slam - NFL"]["missed_wins"], 1)
        self.assertEqual(stats["Slam - NFL"]["missed_losses"], 1)
        self.assertEqual(stats["Slam - NFL"]["missed_pnl_usdc"], "-1.50")
        self.assertEqual(stats["Syndicate - NFL"]["missed_graded"], 1)
        self.assertEqual(stats["Syndicate - NFL"]["missed_pnl_usdc"], "20.00")

    def test_missed_pnl_uses_each_signals_call_time_unit_not_current_unit(self):
        signals = {
            "old-win": {
                "id": "old-win",
                "source": "Slam - NFL",
                "unit_usdc_at_call": "10.00",
                "target_profit_usdc_at_call": "10.00",
                "unit_usdc": "50.00",
                "target_profit_usdc": "50.00",
                "pick_result": "WIN",
                "pick": _pick(units=1, decimal_odds=2.0),
            },
            "later-loss": {
                "id": "later-loss",
                "source": "Slam - NFL",
                "unit_usdc_at_call": "20.00",
                "target_profit_usdc_at_call": "40.00",
                "stake_usdc_at_call": "40.00",
                "unit_usdc": "50.00",
                "target_profit_usdc": "100.00",
                "stake_usdc": "100.00",
                "pick_result": "LOSS",
                "pick": _pick(units=2, decimal_odds=2.0),
            },
        }

        stats = capper._missed_signal_stats(
            signals,
            {},
            unit_usdc=Decimal("10"),
            unit_usdc_by_label={"Slam - NFL": Decimal("50")},
        )

        self.assertEqual(stats["Slam - NFL"]["missed_graded"], 2)
        self.assertEqual(stats["Slam - NFL"]["missed_pnl_usdc"], "-30.00")

    def test_legacy_missed_call_does_not_fall_back_to_current_dynamic_unit(self):
        signals = {
            "legacy-win": {
                "id": "legacy-win",
                "source": "Slam - NFL",
                "pick_result": "WIN",
                "pick": _pick(units=1, decimal_odds=2.0),
            }
        }

        stats = capper._missed_signal_stats(
            signals,
            {},
            unit_usdc=Decimal("10"),
            unit_usdc_by_label={"Slam - NFL": Decimal("75")},
        )

        self.assertEqual(stats["Slam - NFL"]["missed_pnl_usdc"], "10.00")

    def test_paper_execution_does_not_hide_a_missed_real_trade(self):
        signals = {
            "paper-only": {
                "id": "paper-only",
                "source": "Slam - NFL",
                "stake_usdc": "10",
                "pick_result": "WIN",
                "pick": _pick(decimal_odds=2.0),
            }
        }
        executions = {
            "paper": {
                "id": "paper",
                "strategy_pick_id": "paper-only",
                "strategy_source": "Slam - NFL",
                "strategy_sport": "NFL",
                "status": "SETTLED_WIN",
                "paper": True,
            }
        }

        stats = capper._missed_signal_stats(signals, executions)

        self.assertEqual(stats["Slam - NFL"]["missed_graded"], 1)
        self.assertEqual(stats["Slam - NFL"]["missed_pnl_usdc"], "10.00")

    def test_completed_nfl_spread_is_graded_from_final_score(self):
        pick = _pick(
            posted_at="2026-09-26T12:00:00+00:00",
            selection="RAMS -6.5",
            teams=["LAR"],
            bet_types=["spread"],
            spread_lines=["-6.5"],
        )
        payload = {
            "events": [
                {
                    "id": "nfl-game-1",
                    "name": "Los Angeles Rams at Seattle Seahawks",
                    "competitions": [
                        {
                            "status": {"type": {"completed": True}},
                            "competitors": [
                                {
                                    "score": "28",
                                    "team": {
                                        "abbreviation": "LAR",
                                        "displayName": "Los Angeles Rams",
                                        "shortDisplayName": "Rams",
                                    },
                                },
                                {
                                    "score": "20",
                                    "team": {
                                        "abbreviation": "SEA",
                                        "displayName": "Seattle Seahawks",
                                        "shortDisplayName": "Seahawks",
                                    },
                                },
                            ],
                        }
                    ],
                }
            ]
        }

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return payload

        with patch.object(capper.httpx, "get", return_value=_Response()):
            result = capper._nfl_scoreboard_result_for_pick(pick)

        self.assertEqual(result["pick_result"], "WIN")
        self.assertEqual(result["result_source"], "espn_final_score")
        self.assertIn("Rams 28", result["final_score"])


class NflCapperPositionTests(unittest.TestCase):
    def test_open_position_is_sellable_and_finished_position_is_marked(self):
        executions = {
            "open": {
                "id": "open",
                "strategy_source": "Slam - NFL",
                "strategy_sport": "NFL",
                "strategy_selection": "RAMS -6.5",
                "status": "ORDER_SUBMITTED",
                "budget_usdc": "10",
                "quote": {
                    "market": "Rams spread",
                    "market_url": "https://polymarket.com/event/rams",
                    "resolved_outcome": "Rams",
                },
            },
            "win": {
                "id": "win",
                "strategy_source": "Slam - NFL",
                "strategy_sport": "NFL",
                "strategy_selection": "BILLS ML",
                "status": "SETTLED_WIN",
                "budget_usdc": "10",
                "realized_pnl": "8.25",
                "settlement": {"result": "WIN"},
                "quote": {"resolved_outcome": "Bills"},
            },
            "wrong-sport": {
                "id": "wrong-sport",
                "strategy_source": "Slam - NFL",
                "strategy_sport": "CFB",
                "strategy_selection": "ARMY ML",
                "status": "ORDER_SUBMITTED",
                "budget_usdc": "10",
            },
        }

        live_marks = {
            "open": {
                "id": "open",
                "entry_price": "0.50",
                "current_price": "0.60",
                "shares": "20",
                "estimated_pnl": "2.00",
            }
        }
        items = capper._position_items(
            executions,
            "Slam - NFL",
            sport="NFL",
            live_marks=live_marks,
        )
        by_id = {item["trade_id"]: item for item in items}

        self.assertEqual(set(by_id), {"open", "win"})
        self.assertTrue(by_id["open"]["sell_available"])
        self.assertFalse(by_id["open"]["finished"])
        self.assertEqual(by_id["open"]["live_pnl_usdc"], "2.00")
        self.assertEqual(by_id["open"]["live_pnl_pct"], "20.0")
        self.assertEqual(by_id["open"]["current_value_usdc"], "12.00")
        self.assertEqual(by_id["open"]["open_cost_basis_usdc"], "10.00")
        self.assertFalse(by_id["win"]["sell_available"])
        self.assertTrue(by_id["win"]["finished"])
        self.assertEqual(by_id["win"]["result"], "WIN")
        self.assertEqual(by_id["win"]["realized_pnl_usdc"], "8.25")


class NflCapperRecoveryTests(unittest.TestCase):
    def test_recovery_age_is_bounded(self):
        self.assertFalse(capper._is_recovery_age(180, 180, 86400))
        self.assertTrue(capper._is_recovery_age(181, 180, 86400))
        self.assertTrue(capper._is_recovery_age(86400, 180, 86400))
        self.assertFalse(capper._is_recovery_age(86401, 180, 86400))

    def test_recovery_phase_requires_confirmed_pregame(self):
        future = SimpleNamespace(
            state=SimpleNamespace(closed=False, ended=False, live=False),
            schedule=SimpleNamespace(start_time="2026-09-27T17:00:00+00:00"),
            sports=SimpleNamespace(game_status="scheduled"),
        )
        market = SimpleNamespace(
            state=SimpleNamespace(accepting_orders=True),
            sports=SimpleNamespace(game_status="scheduled"),
        )
        now = capper._parse_iso("2026-09-27T12:00:00+00:00")
        self.assertEqual(capper._matched_event_phase(future, market, now), "PREGAME")
        self.assertEqual(
            capper._matched_event_phase(
                future,
                market,
                capper._parse_iso("2026-09-27T18:00:00+00:00"),
            ),
            "LIVE",
        )

    def test_recovery_phase_fails_closed_without_kickoff_metadata(self):
        event = SimpleNamespace(
            state=SimpleNamespace(closed=False, ended=False, live=False),
            schedule=SimpleNamespace(),
            sports=SimpleNamespace(game_status="scheduled"),
        )
        market = SimpleNamespace(
            state=SimpleNamespace(accepting_orders=True),
            sports=SimpleNamespace(game_status="scheduled"),
        )
        self.assertEqual(capper._matched_event_phase(event, market), "UNKNOWN")


class NflStructuredSpreadAndDateResolutionTests(unittest.TestCase):
    @staticmethod
    def _moneyline_event(slug: str, title: str):
        first, second = title.split(" vs ")
        yes = SimpleNamespace(label=first, token_id=slug + "-first")
        no = SimpleNamespace(label=second, token_id=slug + "-second")
        market = SimpleNamespace(
            id=slug + "-ml",
            question=title + " moneyline",
            slug=slug + "-moneyline",
            sports=SimpleNamespace(sports_market_type="moneyline", line=None),
            outcomes=SimpleNamespace(yes=yes, no=no),
            state=SimpleNamespace(accepting_orders=True),
        )
        return SimpleNamespace(id=slug, slug=slug, title=title, markets=[market])

    def test_complementary_structured_spread_matches_selected_team(self):
        bengals = SimpleNamespace(label="Bengals", token_id="cin")
        steelers = SimpleNamespace(label="Steelers", token_id="pit")
        market = SimpleNamespace(
            id="cin-pit-spread-3-5",
            question="Spread: Bengals (-3.5)",
            slug="nfl-cin-pit-2026-09-27-spread-away-3pt5",
            sports=SimpleNamespace(sports_market_type="spread", line=-3.5),
            outcomes=SimpleNamespace(yes=bengals, no=steelers),
            state=SimpleNamespace(accepting_orders=True),
        )
        pick = _pick(
            selection="Steelers +3.5",
            teams=["PIT"],
            bet_types=["spread"],
            spread_lines=["+3.5"],
        )
        label, outcome = capper._select_outcome(market, pick, "spread")
        self.assertEqual(label, "Steelers")
        self.assertEqual(outcome.token_id, "pit")

    def test_better_spread_fallback_chooses_nearest_safer_same_game_line(self):
        cowboys_65 = SimpleNamespace(label="Cowboys", token_id="dal-65")
        commanders_65 = SimpleNamespace(label="Commanders", token_id="was-65")
        market_65 = SimpleNamespace(
            id="dal-was-spread-6-5",
            question="Spread: Cowboys (-6.5)",
            slug="nfl-dal-was-2026-09-27-spread-6pt5",
            sports=SimpleNamespace(sports_market_type="spread", line=-6.5),
            outcomes=SimpleNamespace(yes=cowboys_65, no=commanders_65),
            state=SimpleNamespace(accepting_orders=True),
        )
        cowboys_75 = SimpleNamespace(label="Cowboys", token_id="dal-75")
        commanders_75 = SimpleNamespace(label="Commanders", token_id="was-75")
        market_75 = SimpleNamespace(
            id="dal-was-spread-7-5",
            question="Spread: Cowboys (-7.5)",
            slug="nfl-dal-was-2026-09-27-spread-7pt5",
            sports=SimpleNamespace(sports_market_type="spread", line=-7.5),
            outcomes=SimpleNamespace(yes=cowboys_75, no=commanders_75),
            state=SimpleNamespace(accepting_orders=True),
        )
        event = SimpleNamespace(
            id="dal-was-2026-09-27",
            slug="nfl-dal-was-2026-09-27",
            title="Dallas Cowboys vs Washington Commanders",
            markets=[market_65, market_75],
        )
        pick = _pick(
            posted_at="2026-09-27T18:54:45+00:00",
            selection="COMMANDERS +7",
            teams=["WAS"],
            bet_types=["spread"],
            spread_lines=["+7"],
        )

        with patch.object(
            capper,
            "PublicClient",
            return_value=NflCapperMarketResolutionTests._client([event]),
        ):
            matched_event, matched_market, label, outcome, line = capper._find_better_spread_fallback(
                pick,
                max_distance=Decimal("1.0"),
            )

        self.assertEqual(matched_event.slug, "nfl-dal-was-2026-09-27")
        self.assertEqual(matched_market.id, "dal-was-spread-7-5")
        self.assertEqual(label, "Commanders")
        self.assertEqual(outcome.token_id, "was-75")
        self.assertEqual(line, Decimal("7.5"))

    def test_better_spread_fallback_rejects_worse_or_too_distant_lines(self):
        cowboys_65 = SimpleNamespace(label="Cowboys", token_id="dal-65")
        commanders_65 = SimpleNamespace(label="Commanders", token_id="was-65")
        market_65 = SimpleNamespace(
            id="dal-was-spread-6-5",
            question="Spread: Cowboys (-6.5)",
            slug="nfl-dal-was-2026-09-27-spread-6pt5",
            sports=SimpleNamespace(sports_market_type="spread", line=-6.5),
            outcomes=SimpleNamespace(yes=cowboys_65, no=commanders_65),
            state=SimpleNamespace(accepting_orders=True),
        )
        cowboys_85 = SimpleNamespace(label="Cowboys", token_id="dal-85")
        commanders_85 = SimpleNamespace(label="Commanders", token_id="was-85")
        market_85 = SimpleNamespace(
            id="dal-was-spread-8-5",
            question="Spread: Cowboys (-8.5)",
            slug="nfl-dal-was-2026-09-27-spread-8pt5",
            sports=SimpleNamespace(sports_market_type="spread", line=-8.5),
            outcomes=SimpleNamespace(yes=cowboys_85, no=commanders_85),
            state=SimpleNamespace(accepting_orders=True),
        )
        event = SimpleNamespace(
            id="dal-was-2026-09-27",
            slug="nfl-dal-was-2026-09-27",
            title="Dallas Cowboys vs Washington Commanders",
            markets=[market_65, market_85],
        )
        pick = _pick(
            posted_at="2026-09-27T18:54:45+00:00",
            selection="COMMANDERS +7",
            teams=["WAS"],
            bet_types=["spread"],
            spread_lines=["+7"],
        )

        with patch.object(
            capper,
            "PublicClient",
            return_value=NflCapperMarketResolutionTests._client([event]),
        ):
            with self.assertRaisesRegex(ValueError, "No safer same-game NFL spread"):
                capper._find_better_spread_fallback(
                    pick,
                    max_distance=Decimal("1.0"),
                )

    def test_one_team_moneyline_narrows_to_current_week_event(self):
        current = self._moneyline_event(
            "nfl-min-tb-2026-09-27",
            "Minnesota Vikings vs Tampa Bay Buccaneers",
        )
        future = self._moneyline_event(
            "nfl-min-gb-2026-10-04",
            "Minnesota Vikings vs Green Bay Packers",
        )

        class _Result:
            def __init__(self, items):
                self.items = items
            def first_page(self):
                return self

        class _Client:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def list_events(self, **kwargs):
                return _Result([current, future])

        pick = _pick(
            source="The Syndicate",
            posted_at="2026-09-26T15:40:24+00:00",
            selection="Vikings To Win",
            teams=["MIN"],
            bet_types=["moneyline"],
            spread_lines=[],
        )
        with patch.object(capper, "PublicClient", return_value=_Client()):
            event, _, label, _ = capper._find_market(pick, "moneyline")
        self.assertEqual(event.slug, "nfl-min-tb-2026-09-27")
        self.assertEqual(label, "Minnesota Vikings")



class NflBetterTotalFallbackTests(unittest.TestCase):
    def test_under_42_automatically_uses_under_42_5(self):
        over = SimpleNamespace(label="Over", token_id="over-42-5")
        under = SimpleNamespace(label="Under", token_id="under-42-5")
        market = SimpleNamespace(
            id="chi-phi-total-42-5",
            question="Bears vs Eagles total 42.5",
            slug="nfl-chi-phi-2026-09-29-total-42pt5",
            sports=SimpleNamespace(sports_market_type="total", line=42.5),
            outcomes=SimpleNamespace(yes=over, no=under),
            state=SimpleNamespace(accepting_orders=True),
        )
        event = SimpleNamespace(
            id="chi-phi-2026-09-29",
            slug="nfl-chi-phi-2026-09-29",
            title="Chicago Bears vs Philadelphia Eagles",
            markets=[market],
        )
        pick = _pick(
            posted_at="2026-09-29T00:00:00+00:00",
            selection="UNDER 42",
            teams=["CHI", "PHI"],
            bet_types=["total"],
            total_side="UNDER",
            total_line="42",
        )

        with patch.object(
            capper,
            "PublicClient",
            return_value=NflCapperMarketResolutionTests._client([event]),
        ):
            matched_event, matched_market, label, outcome, line = capper._find_better_total_fallback(pick)

        self.assertEqual(matched_event.slug, "nfl-chi-phi-2026-09-29")
        self.assertEqual(matched_market.id, "chi-phi-total-42-5")
        self.assertEqual(label, "Under")
        self.assertEqual(outcome.token_id, "under-42-5")
        self.assertEqual(line, Decimal("42.5"))

    def test_under_does_not_fallback_to_worse_lower_total(self):
        over = SimpleNamespace(label="Over", token_id="over-41-5")
        under = SimpleNamespace(label="Under", token_id="under-41-5")
        market = SimpleNamespace(
            id="chi-phi-total-41-5",
            question="Bears vs Eagles total 41.5",
            slug="nfl-chi-phi-2026-09-29-total-41pt5",
            sports=SimpleNamespace(sports_market_type="total", line=41.5),
            outcomes=SimpleNamespace(yes=over, no=under),
            state=SimpleNamespace(accepting_orders=True),
        )
        event = SimpleNamespace(
            id="chi-phi-2026-09-29",
            slug="nfl-chi-phi-2026-09-29",
            title="Chicago Bears vs Philadelphia Eagles",
            markets=[market],
        )
        pick = _pick(
            selection="UNDER 42",
            teams=["CHI", "PHI"],
            bet_types=["total"],
            total_side="UNDER",
            total_line="42",
        )
        with patch.object(
            capper,
            "PublicClient",
            return_value=NflCapperMarketResolutionTests._client([event]),
        ):
            with self.assertRaisesRegex(ValueError, "No safer same-game NFL total"):
                capper._find_better_total_fallback(pick)



    def test_team_total_tag_classifies_as_total_market(self):
        kind, reason = capper._classify_pick(
            _pick(
                selection="STEELERS TEAM TOTAL UNDER 21.5",
                teams=["PIT"],
                bet_types=["team_total"],
                spread_lines=[],
                total_side="UNDER",
                total_line=21.5,
            )
        )
        self.assertEqual(kind, "total")
        self.assertIsNone(reason)

    def test_low_one_team_total_infers_team_total_scope(self):
        pick = _pick(
            selection="STEELERS UNDER 21.5",
            teams=["PIT"],
            bet_types=["total"],
            spread_lines=[],
            total_side="UNDER",
            total_line=21.5,
        )
        self.assertTrue(capper._is_team_total_pick(pick))

    def test_team_total_exact_match_rejects_game_total(self):
        game_over = SimpleNamespace(label="Over", token_id="game-over")
        game_under = SimpleNamespace(label="Under", token_id="game-under")
        team_over = SimpleNamespace(label="Over", token_id="pit-over")
        team_under = SimpleNamespace(label="Under", token_id="pit-under")
        game_market = SimpleNamespace(
            id="pit-ne-game-total",
            question="Steelers vs Patriots: O/U 21.5",
            slug="nfl-pit-ne-game-total-21pt5",
            sports=SimpleNamespace(sports_market_type="total", line=21.5),
            outcomes=SimpleNamespace(yes=game_over, no=game_under),
            state=SimpleNamespace(accepting_orders=True),
        )
        team_market = SimpleNamespace(
            id="pit-team-total",
            question="Steelers Team Total: O/U 21.5",
            slug="nfl-pit-ne-steelers-team-total-21pt5",
            sports=SimpleNamespace(sports_market_type="total", line=21.5),
            outcomes=SimpleNamespace(yes=team_over, no=team_under),
            state=SimpleNamespace(accepting_orders=True),
        )
        event = SimpleNamespace(
            id="pit-ne-2026-10-02",
            slug="nfl-pit-ne-2026-10-02",
            title="Pittsburgh Steelers vs New England Patriots",
            markets=[game_market, team_market],
        )
        pick = _pick(
            posted_at="2026-10-02T00:00:00+00:00",
            selection="STEELERS UNDER 21.5",
            teams=["PIT"],
            bet_types=["total"],
            spread_lines=[],
            total_side="UNDER",
            total_line=21.5,
        )
        with patch.object(
            capper,
            "PublicClient",
            return_value=NflCapperMarketResolutionTests._client([event]),
        ):
            _, matched_market, label, outcome = capper._find_market(pick, "total")
        self.assertEqual(matched_market.id, "pit-team-total")
        self.assertEqual(label, "Under")
        self.assertEqual(outcome.token_id, "pit-under")

    def test_team_total_under_21_5_falls_forward_to_23_5(self):
        over = SimpleNamespace(label="Over", token_id="pit-over-23-5")
        under = SimpleNamespace(label="Under", token_id="pit-under-23-5")
        market = SimpleNamespace(
            id="pit-team-total-23-5",
            question="Steelers Team Total: O/U 23.5",
            slug="nfl-pit-ne-steelers-team-total-23pt5",
            sports=SimpleNamespace(sports_market_type="total", line=23.5),
            outcomes=SimpleNamespace(yes=over, no=under),
            state=SimpleNamespace(accepting_orders=True),
        )
        event = SimpleNamespace(
            id="pit-ne-2026-10-02",
            slug="nfl-pit-ne-2026-10-02",
            title="Pittsburgh Steelers vs New England Patriots",
            markets=[market],
        )
        pick = _pick(
            posted_at="2026-10-02T00:00:00+00:00",
            selection="STEELERS UNDER 21.5",
            teams=["PIT"],
            bet_types=["total"],
            spread_lines=[],
            total_side="UNDER",
            total_line=21.5,
        )
        with patch.object(
            capper,
            "PublicClient",
            return_value=NflCapperMarketResolutionTests._client([event]),
        ):
            _, matched_market, label, outcome, line = capper._find_better_total_fallback(
                pick,
                max_distance=Decimal("2.0"),
            )
        self.assertEqual(matched_market.id, "pit-team-total-23-5")
        self.assertEqual(label, "Under")
        self.assertEqual(outcome.token_id, "pit-under-23-5")
        self.assertEqual(line, Decimal("23.5"))

    def test_team_total_fallback_blocks_more_than_two_points(self):
        over = SimpleNamespace(label="Over", token_id="pit-over-24-5")
        under = SimpleNamespace(label="Under", token_id="pit-under-24-5")
        market = SimpleNamespace(
            id="pit-team-total-24-5",
            question="Steelers Team Total: O/U 24.5",
            slug="nfl-pit-ne-steelers-team-total-24pt5",
            sports=SimpleNamespace(sports_market_type="total", line=24.5),
            outcomes=SimpleNamespace(yes=over, no=under),
            state=SimpleNamespace(accepting_orders=True),
        )
        event = SimpleNamespace(
            id="pit-ne-2026-10-02",
            slug="nfl-pit-ne-2026-10-02",
            title="Pittsburgh Steelers vs New England Patriots",
            markets=[market],
        )
        pick = _pick(
            selection="STEELERS UNDER 21.5",
            teams=["PIT"],
            bet_types=["total"],
            spread_lines=[],
            total_side="UNDER",
            total_line=21.5,
        )
        with patch.object(
            capper,
            "PublicClient",
            return_value=NflCapperMarketResolutionTests._client([event]),
        ):
            with self.assertRaisesRegex(ValueError, "No safer same-game NFL total"):
                capper._find_better_total_fallback(
                    pick,
                    max_distance=Decimal("2.0"),
                )


class NflNoFillRetryTests(unittest.TestCase):
    def test_zero_fill_queue_result_is_retryable(self):
        self.assertTrue(
            capper._queue_result_is_no_fill(
                {
                    "action": "BUY",
                    "status": "DONE",
                    "result": {
                        "ok": False,
                        "filled_shares": "0",
                        "status": "TEST_BUY_UNFILLED_CANCELED",
                    },
                }
            )
        )

    def test_positive_fill_is_not_no_fill(self):
        self.assertFalse(
            capper._queue_result_is_no_fill(
                {
                    "action": "BUY",
                    "status": "DONE",
                    "result": {"ok": True, "filled_shares": "2.5", "status": "ORDER_SUBMITTED"},
                }
            )
        )


class NflDashboardSignalItemTests(unittest.TestCase):
    def test_graded_untraded_signal_is_closed_and_marked_not_executed(self):
        record = {
            "id": "signal-1",
            "source": "Slam - NFL",
            "selection": "BRONCOS +1.5",
            "posted_at": "2026-09-27T23:00:00+00:00",
            "status": "EXECUTOR_FAILED",
            "units": "3",
            "stake_usdc": "30.00",
            "pick_result": "WIN",
            "result_event_title": "Rams at Broncos",
            "final_score": "Rams 26 - Broncos 30",
        }

        item = capper._nfl_signal_dashboard_item(record, {})

        self.assertEqual(item["event_phase"], "CLOSED")
        self.assertEqual(item["pick_result"], "WIN")
        self.assertFalse(item["trade_executed"])
        self.assertIsNone(item["trade_id"])

    def test_signal_item_links_execution_for_trade_result_and_sell_state(self):
        record = {
            "id": "signal-2",
            "source": "Syndicate - NFL",
            "selection": "EAGLES ML",
            "posted_at": "2026-09-28T20:00:00+00:00",
            "status": "EXECUTOR_DONE",
            "market": "Eagles vs Bears moneyline",
            "market_url": "https://polymarket.com/sports/nfl/nfl-phi-chi-2026-09-28",
            "outcome": "Philadelphia Eagles",
            "asset_id": "asset-1",
        }
        executions = {
            "trade-2": {
                "id": "trade-2",
                "strategy_pick_id": "signal-2",
                "strategy_source": "Syndicate - NFL",
                "strategy_sport": "NFL",
                "status": "ORDER_SUBMITTED",
                "budget_usdc": "10.00",
            }
        }

        item = capper._nfl_signal_dashboard_item(record, executions)

        self.assertEqual(item["match_status"], "MATCHED")
        self.assertTrue(item["trade_executed"])
        self.assertEqual(item["trade_id"], "trade-2")
        self.assertTrue(item["sell_available"])



class NflRollingPnlStatsTests(unittest.TestCase):
    def test_stats_include_last_7_and_30_day_realized_pnl(self):
        now = datetime.now(timezone.utc)
        executions = {
            "recent": {
                "id": "recent",
                "strategy_source": "Slam - NFL",
                "strategy_sport": "NFL",
                "status": "SETTLED_WIN",
                "realized_pnl": "5.00",
                "actual_cost_usdc": "10.00",
                "closed_at": (now - timedelta(days=2)).isoformat(),
            },
            "mid": {
                "id": "mid",
                "strategy_source": "Slam - NFL",
                "strategy_sport": "NFL",
                "status": "SETTLED_WIN",
                "realized_pnl": "3.00",
                "actual_cost_usdc": "10.00",
                "closed_at": (now - timedelta(days=15)).isoformat(),
            },
            "old": {
                "id": "old",
                "strategy_source": "Slam - NFL",
                "strategy_sport": "NFL",
                "status": "SETTLED_LOSS",
                "realized_pnl": "-4.00",
                "actual_cost_usdc": "10.00",
                "closed_at": (now - timedelta(days=45)).isoformat(),
            },
        }

        stats = capper._stats_from_executions(
            executions,
            labels=("Slam - NFL",),
            sport="NFL",
        )["Slam - NFL"]

        self.assertEqual(stats["realized_pnl_usdc"], "4.00")
        self.assertEqual(stats["realized_pnl_7d_usdc"], "5.00")
        self.assertEqual(stats["realized_pnl_30d_usdc"], "8.00")


if __name__ == "__main__":
    unittest.main()



def test_nfl_capper_dashboard_has_persistent_online_controls():
    source = Path("app/nfl_capper_ingest.py").read_text(encoding="utf-8")

    assert '"/api/nfl-cappers/sport-enabled"' in source
    assert '"/api/nfl-cappers/enabled/{capper_key}"' in source
    assert "capper_control.is_enabled(core, SPORT_CONTROL_LABEL)" in source
    assert "capper_control.is_enabled(core, source_label)" in source
    assert '"PAUSED_SPORT"' in source
    assert '"PAUSED_CAPPER"' in source
    assert "nflToggleSport" in source
    assert "nflToggleCapper" in source
    assert '<div class="capper-panel-title">NFL AUTO-TRADING</div>' in source
    assert 'id="nflSportPower"' in source
    assert 'id="nflCapperPower-slam"' in source
    assert 'id="nflCapperPower-syndicate"' in source


def test_nfl_event_badges_show_event_timing():
    source = Path("app/nfl_capper_ingest.py").read_text(encoding="utf-8")

    assert "function nflEventBadgeLabel" in source
    assert "return 'LIVE'" in source
    assert "'T-'+nflEventDelta" in source
    assert "'COMPLETED · '+nflEventDelta" in source
    assert '"event_completed_at": (' in source


def test_nfl_section_labels_are_not_duplicated_and_use_shared_badges():
    source = Path("app/nfl_capper_ingest.py").read_text(encoding="utf-8")

    assert "spec[1]+' signals'" not in source
    assert "Signals signals" not in source
    assert '<span class="capper-section-badge">Open positions</span>' in source
    assert '<span class="capper-section-badge">Settled positions' in source
    assert 'class="capper-event-badge"' in source
    assert "nflPickList(spec[1],spec[3],spec[4])" in source


def test_nfl_signals_use_wnba_monitor_card_structure():
    source = Path("app/nfl_capper_ingest.py").read_text(encoding="utf-8")

    assert 'return \'<div class="monitor-signal"><b>\'' in source
    assert 'class="monitor-meta"' in source
    assert 'class="monitor-action ' in source
    assert 'class="monitor-signal-actions"' in source
    assert "border-radius:8px;'+visual.row" not in source
    assert 'class="nfl-result-line' not in source


def test_nfl_sizing_controls_put_mode_buttons_left_of_inputs():
    source = Path("app/nfl_capper_ingest.py").read_text(encoding="utf-8")

    assert '<span class="capper-sizing-label">Fixed 1u win $</span>' in source
    assert '<span class="capper-sizing-label">Portfolio %</span>' in source
    fixed = source.index('onclick="nflSetUnitSize')
    fixed_input = source.index('id="nflUnitSize-')
    auto = source.index('onclick="nflSetPortfolioPct')
    auto_input = source.index('id="nflPortfolioPct-')
    assert fixed < fixed_input
    assert auto < auto_input
    assert '<span>$</span>' not in source
    assert '<span>%</span>' not in source


def test_nfl_open_positions_label_stake_and_shares_consistently():
    source = Path("app/nfl_capper_ingest.py").read_text(encoding="utf-8")
    assert "Stake $'+Number(cost||0).toFixed(2)+' · Shares '+shares" in source
    compile(source, "app/nfl_capper_ingest.py", "exec")
