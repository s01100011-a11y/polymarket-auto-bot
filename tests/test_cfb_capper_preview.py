from __future__ import annotations

import unittest
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app import cfb_capper_preview as capper


def _pick(**overrides):
    row = {
        "source": "SLAM - All Access",
        "source_key": "slam",
        "posted_at": "2026-09-25T04:00:00+00:00",
        "selection": "BAYLOR +7",
        "team_hint": "BAYLOR",
        "event_hints": ["BAYLOR"],
        "bet_types": ["spread"],
        "period": None,
        "spread_lines": ["+7"],
        "total_side": None,
        "total_line": None,
        "units": 2,
        "status": "active",
    }
    row.update(overrides)
    return row


class CfbSourceAndSizingTests(unittest.TestCase):
    def test_sources_are_tracked_separately(self):
        self.assertEqual(capper._source_label(_pick(source="SLAM - All Access")), "Slam - CFB")
        self.assertEqual(capper._source_label(_pick(source="The Syndicate")), "Syndicate - CFB")
        self.assertIsNone(capper._source_label(_pick(source="Blacksmith Bets Standard VIP")))

    def test_posted_units_are_profit_targets_not_flat_stakes(self):
        self.assertEqual(capper.nfl._target_profit_for_pick(_pick(units=2)), Decimal("20.00"))
        self.assertEqual(capper.nfl._target_profit_for_pick(_pick(units=None)), Decimal("10.00"))
        self.assertEqual(capper._stake_for_pick(_pick(units=2)), Decimal("23.00"))
        self.assertEqual(capper._stake_for_pick(_pick(units=None)), Decimal("11.50"))

    def test_manual_fallback_is_marked_and_keeps_normal_source_mapping(self):
        rows = capper._manual_fallback_picks(
            '[{"source":"The Syndicate","posted_at":"2026-09-26T16:00:00+00:00",'
            '"selection":"Tennessee +7.5","team_hint":"Tennessee",'
            '"event_hints":["Tennessee"],"bet_types":["spread"],'
            '"spread_lines":["+7.5"],"units":1.5,"status":"active"}]'
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["manual_fallback"])
        self.assertEqual(capper._source_label(rows[0]), "Syndicate - CFB")

    def test_manual_fallback_semantic_dedupe_ignores_timestamp_and_odds(self):
        first = _pick(
            source="The Syndicate",
            posted_at="2026-09-26T16:00:00+00:00",
            decimal_odds=1.67,
        )
        later = _pick(
            source="The Syndicate",
            posted_at="2026-09-26T16:05:00+00:00",
            decimal_odds=1.70,
        )
        self.assertEqual(
            capper._semantic_fingerprint(first),
            capper._semantic_fingerprint(later),
        )

    def test_period_and_team_total_fail_closed(self):
        kind, reason = capper._classify_pick(
            _pick(period="1H", bet_types=["spread", "period"])
        )
        self.assertIsNone(kind)
        self.assertIn("period", reason)

        kind, reason = capper._classify_pick(
            _pick(
                selection="BAYLOR TEAM TOTAL OVER 24.5",
                bet_types=["team_total"],
                team_hint=None,
                event_hints=[],
                total_side="OVER",
                total_line=24.5,
            )
        )
        self.assertIsNone(kind)
        self.assertIn("team totals", reason)

    def test_total_requires_explicit_two_team_matchup(self):
        kind, reason = capper._classify_pick(
            _pick(
                selection="UNDER 52.5",
                bet_types=["total"],
                team_hint=None,
                event_hints=[],
                spread_lines=[],
                total_side="UNDER",
                total_line=52.5,
            )
        )
        self.assertIsNone(kind)
        self.assertIn("two-team matchup", reason)

        kind, reason = capper._classify_pick(
            _pick(
                selection="UNDER 52.5",
                bet_types=["total"],
                team_hint=None,
                event_hints=["Texas", "Tennessee"],
                spread_lines=[],
                total_side="UNDER",
                total_line=52.5,
            )
        )
        self.assertEqual(kind, "total")
        self.assertIsNone(reason)


class CfbMarketResolutionTests(unittest.TestCase):
    @staticmethod
    def _result(items):
        class _Result:
            def __init__(self, rows):
                self.items = rows

            def first_page(self):
                return self

        return _Result(items)

    def test_total_resolves_unique_cfb_event_and_structured_line(self):
        over = SimpleNamespace(label="Over", token_id="over-token")
        under = SimpleNamespace(label="Under", token_id="under-token")
        market = SimpleNamespace(
            id="tx-tenn-total-52-5",
            question="Texas vs Tennessee: O/U 52.5",
            slug="cfb-tx-tenn-total-52-5",
            sports=SimpleNamespace(sports_market_type="total", line=52.5),
            outcomes=SimpleNamespace(yes=over, no=under),
            state=SimpleNamespace(accepting_orders=True),
        )
        event = SimpleNamespace(
            id="tx-tenn",
            slug="cfb-tx-tenn-2026-09-26",
            title="Texas vs Tennessee",
            markets=[market],
        )

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def list_events(self, **kwargs):
                return CfbMarketResolutionTests._result([event])

        pick = _pick(
            selection="UNDER 52.5",
            bet_types=["total"],
            team_hint=None,
            event_hints=["Texas", "Tennessee"],
            spread_lines=[],
            total_side="UNDER",
            total_line=52.5,
        )
        with patch.object(capper, "PublicClient", return_value=_Client()):
            matched_event, matched_market, label, outcome = capper._find_market(pick, "total")

        self.assertEqual(matched_event.slug, "cfb-tx-tenn-2026-09-26")
        self.assertEqual(matched_market.id, "tx-tenn-total-52-5")
        self.assertEqual(label, "Under")
        self.assertEqual(outcome.token_id, "under-token")

    def test_moneyline_uses_two_team_matchup_to_select_one_event(self):
        yes = SimpleNamespace(label="Texas", token_id="texas-token")
        no = SimpleNamespace(label="Tennessee", token_id="tenn-token")
        market = SimpleNamespace(
            id="tx-tenn-ml",
            question="Texas vs Tennessee",
            slug="cfb-tx-tenn-moneyline",
            sports=SimpleNamespace(sports_market_type="moneyline", line=None),
            outcomes=SimpleNamespace(yes=yes, no=no),
            state=SimpleNamespace(accepting_orders=True),
        )
        target = SimpleNamespace(
            id="tx-tenn",
            slug="cfb-tx-tenn-2026-09-26",
            title="Texas vs Tennessee",
            markets=[market],
        )
        other = SimpleNamespace(
            id="tx-other",
            slug="cfb-tx-other-2026-10-03",
            title="Texas vs Other",
            markets=[market],
        )

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def list_events(self, **kwargs):
                query = str(kwargs.get("title_search") or "").casefold()
                if query == "texas":
                    return CfbMarketResolutionTests._result([target, other])
                if query == "tennessee":
                    return CfbMarketResolutionTests._result([target])
                return CfbMarketResolutionTests._result([])

        pick = _pick(
            selection="TEXAS ML",
            team_hint="Texas",
            event_hints=["Texas", "Tennessee"],
            bet_types=["moneyline"],
            spread_lines=[],
        )
        with patch.object(capper, "PublicClient", return_value=_Client()):
            event, _, label, outcome = capper._find_market(pick, "moneyline")

        self.assertEqual(event.slug, "cfb-tx-tenn-2026-09-26")
        self.assertEqual(label, "Texas")
        self.assertEqual(outcome.token_id, "texas-token")

    def test_one_team_pick_uses_posted_date_to_select_nearby_event(self):
        def event(slug, title):
            yes = SimpleNamespace(label="Clemson", token_id=slug + "-yes")
            no = SimpleNamespace(label="Opponent", token_id=slug + "-no")
            market = SimpleNamespace(
                id=slug + "-ml",
                question=title,
                slug=slug + "-ml",
                sports=SimpleNamespace(sports_market_type="moneyline", line=None),
                outcomes=SimpleNamespace(yes=yes, no=no),
                state=SimpleNamespace(accepting_orders=True),
            )
            return SimpleNamespace(
                id=slug,
                slug=slug,
                title=title,
                markets=[market],
            )

        target = event("cfb-clemson-unc-2026-09-26", "Clemson vs UNC")
        future = event("cfb-clemson-fsu-2026-10-03", "Clemson vs FSU")

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def list_events(self, **kwargs):
                return CfbMarketResolutionTests._result([target, future])

        pick = _pick(
            selection="CLEMSON ML -135",
            posted_at="2026-09-25T23:30:00+00:00",
            team_hint="Clemson",
            event_hints=["Clemson"],
            bet_types=["moneyline"],
            spread_lines=[],
        )
        with patch.object(capper, "PublicClient", return_value=_Client()):
            matched_event, _, label, outcome = capper._find_market(pick, "moneyline")

        self.assertEqual(matched_event.slug, "cfb-clemson-unc-2026-09-26")
        self.assertEqual(label, "Clemson")
        self.assertEqual(outcome.token_id, "cfb-clemson-unc-2026-09-26-yes")

    def test_week_later_event_does_not_inherit_old_stale_pick(self):
        def event(slug):
            yes = SimpleNamespace(label="Clemson", token_id=slug + "-yes")
            no = SimpleNamespace(label="Opponent", token_id=slug + "-no")
            market = SimpleNamespace(
                id=slug + "-ml",
                question="Clemson moneyline",
                slug=slug + "-ml",
                sports=SimpleNamespace(sports_market_type="moneyline", line=None),
                outcomes=SimpleNamespace(yes=yes, no=no),
                state=SimpleNamespace(accepting_orders=True),
            )
            return SimpleNamespace(
                id=slug,
                slug=slug,
                title="Clemson vs Opponent",
                markets=[market],
            )

        events = [
            event("cfb-clemson-a-2026-10-03"),
            event("cfb-clemson-b-2026-10-10"),
        ]

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def list_events(self, **kwargs):
                return CfbMarketResolutionTests._result(events)

        pick = _pick(
            selection="CLEMSON ML",
            posted_at="2026-09-25T23:30:00+00:00",
            team_hint="Clemson",
            event_hints=["Clemson"],
            bet_types=["moneyline"],
            spread_lines=[],
        )
        with patch.object(capper, "PublicClient", return_value=_Client()):
            with self.assertRaisesRegex(ValueError, "No nearby dated"):
                capper._find_market(pick, "moneyline")

    def test_single_future_event_does_not_inherit_old_pick(self):
        yes = SimpleNamespace(label="Temple", token_id="future-temple-yes")
        no = SimpleNamespace(label="Opponent", token_id="future-temple-no")
        market = SimpleNamespace(
            id="future-temple-ml",
            question="Temple moneyline",
            slug="future-temple-ml",
            sports=SimpleNamespace(sports_market_type="moneyline", line=None),
            outcomes=SimpleNamespace(yes=yes, no=no),
            state=SimpleNamespace(accepting_orders=True),
        )
        future = SimpleNamespace(
            id="future-temple",
            slug="cfb-temple-opponent-2026-10-03",
            title="Temple vs Opponent",
            markets=[market],
        )

        class _Client:
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False
            def list_events(self, **kwargs):
                return CfbMarketResolutionTests._result([future])

        pick = _pick(
            selection="TEMPLE ML",
            posted_at="2026-09-25T19:46:30+00:00",
            team_hint="Temple",
            event_hints=["Temple"],
            bet_types=["moneyline"],
            spread_lines=[],
        )
        with patch.object(capper, "PublicClient", return_value=_Client()):
            with self.assertRaisesRegex(ValueError, "No nearby dated"):
                capper._find_market(pick, "moneyline")

    def test_multiple_matching_events_fail_closed(self):
        def event(slug):
            yes = SimpleNamespace(label="Baylor", token_id=slug + "-yes")
            no = SimpleNamespace(label="Opponent", token_id=slug + "-no")
            market = SimpleNamespace(
                id=slug + "-ml",
                question="Baylor moneyline",
                slug=slug + "-ml",
                sports=SimpleNamespace(sports_market_type="moneyline", line=None),
                outcomes=SimpleNamespace(yes=yes, no=no),
                state=SimpleNamespace(accepting_orders=True),
            )
            return SimpleNamespace(
                id=slug,
                slug=slug,
                title="Baylor vs Opponent",
                markets=[market],
            )

        events = [
            event("cfb-baylor-a-2026-09-25"),
            event("cfb-baylor-b-2026-09-25"),
        ]

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def list_events(self, **kwargs):
                return CfbMarketResolutionTests._result(events)

        pick = _pick(
            selection="BAYLOR ML",
            bet_types=["moneyline"],
            spread_lines=[],
        )
        with patch.object(capper, "PublicClient", return_value=_Client()):
            with self.assertRaisesRegex(ValueError, "multiple open events"):
                capper._find_market(pick, "moneyline")


class CfbDashboardPanelTests(unittest.TestCase):
    def test_injects_cfb_panel_with_matching_nfl_classes(self):
        html = '<style></style>\n  <div class="tabs"></div>\n<script></script>'
        rendered = capper._inject_dashboard_panel(html)
        self.assertIn('id="cfbCapperStats"', rendered)
        self.assertIn('class="nfl-capper-panel"', rendered)
        self.assertIn('Slam - CFB', rendered)
        self.assertIn('Syndicate - CFB', rendered)
        self.assertIn('/api/cfb-cappers/status', rendered)
        self.assertIn('preview_queued', rendered)
        self.assertIn('all_items', rendered)
        self.assertIn('queued_items', rendered)
        self.assertIn('done_items', rendered)
        self.assertIn('pregame_items', rendered)
        self.assertIn('closed_items', rendered)
        self.assertIn('unsupported_items', rendered)
        self.assertIn("['signals','Signals'", rendered)
        self.assertIn("['queued','Queued'", rendered)
        self.assertIn("['done','Done'", rendered)
        self.assertIn("['live','Live'", rendered)
        self.assertIn('GAME LIVE', rendered)
        self.assertIn('BUY LIVE', rendered)
        self.assertIn('OPEN MARKET', rendered)
        self.assertIn('Matched:', rendered)
        self.assertIn('Live BUY', rendered)
        self.assertIn("status '+cfbEsc(item.status)", rendered)
        self.assertIn('manual pregame/live while market open', rendered)
        self.assertIn('scan ', rendered)
        self.assertIn('/api/cfb-cappers/manual-buy/', rendered)
        self.assertIn('/api/cfb-cappers/manual-buy-alternate/', rendered)
        self.assertIn('/api/cfb-cappers/unit-percent/', rendered)
        self.assertIn('cfbSetPortfolioPct', rendered)
        self.assertIn('% PORTFOLIO', rendered)
        self.assertIn('AUTO %', rendered)
        self.assertIn('portfolio percentage', rendered)
        self.assertIn('BETTER LINE', rendered)
        self.assertIn("label:'PREGAME'", rendered)
        self.assertIn("label:'LIVE'", rendered)
        self.assertIn("label:'FINISHED'", rendered)
        self.assertIn('#3b82f6', rendered)
        self.assertIn('#22c55e', rendered)
        self.assertIn('#94a3b8', rendered)
        self.assertIn('GAME FINISHED', rendered)

    def test_injection_is_idempotent(self):
        html = '<style></style>\n  <div class="tabs"></div>\n<script></script>'
        once = capper._inject_dashboard_panel(html)
        twice = capper._inject_dashboard_panel(once)
        self.assertEqual(once, twice)


    def test_status_pick_items_show_recent_queue_details(self):
        rows = [
            {
                "status": "PREVIEW_QUEUED",
                "selection": "TEXAS ML",
                "units": "1",
                "stake_usdc": "10.00",
                "posted_at": "2026-09-25T22:00:00+00:00",
                "updated_at": "2026-09-25T22:01:00+00:00",
                "market": "Texas vs Tennessee",
                "outcome": "Texas",
                "request_id": "exec-old",
            },
            {
                "status": "PREVIEW_QUEUED",
                "selection": "NAVY -6.5",
                "units": "2",
                "stake_usdc": "20.00",
                "posted_at": "2026-09-25T22:30:00+00:00",
                "updated_at": "2026-09-25T22:31:00+00:00",
                "market": "Navy vs Air Force",
                "outcome": "Navy",
                "request_id": "exec-new",
            },
        ]
        items = capper._recent_status_items(rows, "PREVIEW_QUEUED")
        self.assertEqual([item["selection"] for item in items], ["NAVY -6.5", "TEXAS ML"])
        self.assertEqual(items[0]["stake_usdc"], "20.00")
        self.assertEqual(items[0]["market"], "Navy vs Air Force")

    def test_all_items_keep_status_for_tab_view(self):
        rows = [
            {
                "id": "done",
                "status": "PREVIEW_DONE",
                "selection": "DONE PICK",
                "updated_at": "2026-09-26T01:00:00+00:00",
            },
            {
                "id": "live",
                "status": "MATCHED_LIVE",
                "selection": "LIVE PICK",
                "updated_at": "2026-09-26T02:00:00+00:00",
            },
        ]
        items = capper._recent_all_items(rows)
        self.assertEqual([item["selection"] for item in items], ["LIVE PICK", "DONE PICK"])
        self.assertEqual(items[0]["status"], "MATCHED_LIVE")
        self.assertEqual(items[1]["status"], "PREVIEW_DONE")

    def test_status_pick_items_include_pregame_live_odds(self):
        rows = [
            {
                "id": "clemson",
                "status": "MATCHED_PREGAME",
                "selection": "CLEMSON ML -135",
                "units": "2",
                "stake_usdc": "20.00",
                "posted_at": "2026-09-25T20:00:00+00:00",
                "reason": "outside 180s auto-trade freshness window; manual BUY remains available until event start",
                "match_status": "MATCHED",
                "market_type": "moneyline",
                "market_url": "https://polymarket.com/sports/cfb/cfb-clemson-unc-2026-09-26",
                "outcome": "Clemson",
                "asset_id": "clemson-token",
                "best_ask": "0.58",
                "current_buy_price": "0.57",
                "live_odds_american": "-138",
            }
        ]
        items = capper._recent_status_items(rows, "MATCHED_PREGAME")
        self.assertEqual(items[0]["selection"], "CLEMSON ML -135")
        self.assertEqual(items[0]["best_ask"], "0.58")
        self.assertEqual(items[0]["live_odds_american"], "-138")
        self.assertTrue(items[0]["buy_available"])


class CfbLifecycleAndOddsTests(unittest.TestCase):
    def test_american_odds_from_polymarket_price(self):
        self.assertEqual(capper._american_odds_from_price("0.58"), "-138")
        self.assertEqual(capper._american_odds_from_price("0.40"), "+150")

    def test_future_event_is_pregame_and_past_open_event_is_live(self):
        future = {
            "event_start_at": "2026-09-26T12:00:00+00:00",
            "market_accepting_orders": True,
        }
        past = {
            "event_start_at": "2026-09-26T01:00:00+00:00",
            "market_accepting_orders": True,
        }
        now = capper._parse_iso("2026-09-26T02:00:00+00:00")
        self.assertEqual(capper._event_phase(future, now), "PREGAME")
        self.assertEqual(capper._event_phase(past, now), "LIVE")

    def test_nested_gamma_schedule_and_state_drive_lifecycle(self):
        event = SimpleNamespace(
            schedule=SimpleNamespace(
                start_time=capper._parse_iso("2026-09-25T23:00:00+00:00"),
                finished_at=capper._parse_iso("2026-09-26T02:00:00+00:00"),
                closed_time=None,
            ),
            state=SimpleNamespace(closed=False, ended=True, live=False),
            sports=SimpleNamespace(game_status="Final"),
        )
        market = SimpleNamespace(
            sports=SimpleNamespace(game_start_time=None),
            state=SimpleNamespace(accepting_orders=True),
        )
        meta = capper._event_lifecycle_metadata(event, market)
        self.assertEqual(meta["event_start_at"], "2026-09-25T23:00:00+00:00")
        self.assertEqual(meta["event_finished_at"], "2026-09-26T02:00:00+00:00")
        self.assertTrue(meta["event_ended"])
        self.assertEqual(meta["game_status"], "Final")
        self.assertEqual(
            capper._event_phase(meta, capper._parse_iso("2026-09-26T02:30:00+00:00")),
            "CLOSED",
        )

    def test_event_phase_closes_on_final_status_even_if_market_accepting(self):
        record = {
            "game_status": "Final",
            "market_accepting_orders": True,
            "event_start_at": "2026-09-25T23:00:00+00:00",
        }
        self.assertEqual(capper._event_phase(record), "CLOSED")

    def test_date_only_start_is_not_treated_as_midnight_kickoff(self):
        event = SimpleNamespace(start_date="2026-09-26")
        self.assertIsNone(capper._precise_event_start(event))

    def test_sports_kickoff_beats_generic_event_start_date(self):
        event = SimpleNamespace(start_date="2026-09-26T00:00:00+00:00")
        market = SimpleNamespace(
            sports=SimpleNamespace(game_start_time="2026-09-26T19:30:00+00:00")
        )
        self.assertEqual(
            capper._precise_event_start(event, market),
            capper._parse_iso("2026-09-26T19:30:00+00:00"),
        )


    def test_runtime_refresh_keeps_started_open_market_live(self):
        record = {
            "id": "clemson",
            "status": "EVENT_STARTED",
            "match_status": "MATCHED",
            "market_type": "moneyline",
            "event_slug": "cfb-clemson-unc-2026-09-26",
            "event_title": "Clemson vs UNC",
            "event_start_at": "2020-01-01T00:00:00+00:00",
            "event_start_checked_at": "2026-09-26T00:00:00+00:00",
            "market_accepting_orders": True,
            "market_url": "https://polymarket.com/sports/cfb/cfb-clemson-unc-2026-09-26",
            "outcome": "Clemson",
            "asset_id": "clemson-token",
            "pick": _pick(
                selection="CLEMSON ML",
                team_hint="Clemson",
                event_hints=["Clemson", "UNC"],
                bet_types=["moneyline"],
                spread_lines=[],
            ),
        }
        refreshed = {
            "event_title": "Clemson vs UNC",
            "event_start_at": "2020-01-01T00:00:00+00:00",
            "event_start_checked_at": "2026-09-26T01:00:00+00:00",
            "event_closed": False,
            "event_ended": False,
            "event_live": True,
            "event_finished_at": None,
            "game_status": "Live",
            "market_accepting_orders": True,
        }
        with (
            patch.object(capper, "_refresh_saved_event_state", return_value=refreshed),
            patch.object(capper, "_read_live_buy_quote", return_value={
                "current_buy_price": "0.61",
                "best_ask": "0.62",
                "max_price": "0.62",
                "spread": "0.02",
                "live_odds_american": "-163",
                "quote_updated_at": "2026-09-26T01:00:01+00:00",
            }),
        ):
            changed = capper._refresh_record_runtime(record)

        self.assertTrue(changed)
        self.assertEqual(record["status"], "MATCHED_LIVE")
        self.assertEqual(record["best_ask"], "0.62")
        self.assertEqual(record["live_odds_american"], "-163")

    def test_runtime_rejects_legacy_temple_future_game_rollover(self):
        record = {
            "id": "temple",
            "status": "MATCHED_PREGAME",
            "match_status": "MATCHED",
            "market_type": "spread",
            "event_slug": "cfb-templ-sfl-2026-10-03",
            "event_title": "Temple vs South Florida",
            "event_start_at": "2026-10-03T23:00:00+00:00",
            "event_start_checked_at": "2026-09-26T02:39:00+00:00",
            "market_accepting_orders": True,
            "market_url": "https://polymarket.com/sports/cfb/cfb-templ-sfl-2026-10-03",
            "outcome": "Temple",
            "asset_id": "wrong-future-temple-token",
            "pick": _pick(
                selection="TEMPLE +3.5",
                posted_at="2026-09-25T19:46:30+00:00",
                team_hint="Temple",
                event_hints=["Temple"],
                bet_types=["spread"],
                spread_lines=["+3.5"],
            ),
        }
        self.assertFalse(capper._stored_match_within_pick_window(record))
        with (
            patch.object(
                capper,
                "_resolve_market_match",
                side_effect=ValueError("No nearby dated Polymarket CFB event matched the one-team pick"),
            ),
            patch.object(capper, "_find_spread_alternatives", return_value=[]),
            patch.object(capper, "_read_live_buy_quote", side_effect=AssertionError("wrong future event must never quote")),
        ):
            changed = capper._refresh_record_runtime(record)

        self.assertTrue(changed)
        self.assertEqual(record["status"], "EVENT_CLOSED")
        self.assertEqual(record["event_phase"], "CLOSED")
        self.assertEqual(record["match_status"], "INVALID_FUTURE_MATCH")
        self.assertFalse(record["market_accepting_orders"])
        self.assertIn("future-game match rejected", record["reason"])

    def test_runtime_refresh_moves_finished_pregame_record_to_closed(self):
        record = {
            "id": "temple",
            "status": "MATCHED_PREGAME",
            "match_status": "MATCHED",
            "market_type": "spread",
            "event_slug": "cfb-temple-opponent-2026-09-25",
            "event_title": "Temple vs Opponent",
            "event_start_at": None,
            "event_start_checked_at": "2026-09-25T19:50:00+00:00",
            "market_accepting_orders": True,
            "market_url": "https://polymarket.com/sports/cfb/cfb-temple-opponent-2026-09-25",
            "outcome": "Temple",
            "asset_id": "temple-token",
            "pick": _pick(
                selection="TEMPLE +3.5",
                posted_at="2026-09-25T19:46:30+00:00",
                team_hint="Temple",
                event_hints=["Temple"],
                bet_types=["spread"],
                spread_lines=["+3.5"],
            ),
        }
        refreshed = {
            "event_title": "Temple vs Opponent",
            "event_start_at": "2026-09-25T20:00:00+00:00",
            "event_start_checked_at": "2026-09-26T02:30:00+00:00",
            "event_finished_at": "2026-09-26T00:45:00+00:00",
            "event_closed": False,
            "event_ended": True,
            "event_live": False,
            "game_status": "Final",
            "market_accepting_orders": True,
        }
        with (
            patch.object(capper, "_refresh_saved_event_state", return_value=refreshed),
            patch.object(capper, "_read_live_buy_quote", side_effect=AssertionError("closed game must not quote")),
        ):
            changed = capper._refresh_record_runtime(record)

        self.assertTrue(changed)
        self.assertEqual(record["status"], "EVENT_CLOSED")
        self.assertEqual(record["event_phase"], "CLOSED")
        self.assertEqual(record["game_status"], "Final")


class CfbAlternateLifecycleTests(unittest.TestCase):
    def test_finished_alternate_signal_moves_to_closed(self):
        record = {
            "status": "MATCHED_LIVE_ALTERNATE",
            "event_phase": "LIVE",
            "reason": "exact original spread is unavailable",
            "pick": _pick(
                selection="NORTHWESTERN 21",
                posted_at="2026-09-25T22:32:01+00:00",
                team_hint="Northwestern",
                event_hints=["Northwestern"],
                bet_types=["spread"],
                spread_lines=["+21"],
            ),
            "live_alternatives": [
                {
                    "event_slug": "cfb-nw-ind-2026-09-25",
                    "asset_id": "nw-215-no",
                }
            ],
        }
        with patch.object(
            capper,
            "_refresh_saved_event_state",
            return_value={
                "event_title": "Northwestern vs Indiana",
                "event_start_at": "2026-09-25T23:00:00+00:00",
                "event_start_checked_at": "2026-09-26T02:30:00+00:00",
                "event_finished_at": "2026-09-26T02:00:00+00:00",
                "event_closed": True,
                "event_ended": True,
                "event_live": False,
                "game_status": "Final",
                "market_accepting_orders": False,
            },
        ):
            changed = capper._refresh_spread_alternatives(record)

        self.assertTrue(changed)
        self.assertEqual(record["status"], "EVENT_CLOSED")
        self.assertEqual(record["event_phase"], "CLOSED")


class CfbSpreadAlternateTests(unittest.TestCase):
    @staticmethod
    def _market(
        question,
        yes_label,
        no_label,
        token,
        *,
        line,
        market_type="spreads",
        accepting=True,
    ):
        yes = SimpleNamespace(label=yes_label, token_id=token + "-yes")
        no = SimpleNamespace(label=no_label, token_id=token + "-no")
        return SimpleNamespace(
            id=token + "-market",
            question=question,
            slug=token + "-spread",
            group_item_title=f"Spread {line}",
            sports=SimpleNamespace(
                sports_market_type=market_type,
                line=line,
                game_start_time="2020-01-01T00:00:00+00:00",
            ),
            outcomes=SimpleNamespace(yes=yes, no=no),
            state=SimpleNamespace(accepting_orders=accepting),
        )

    def test_spread_outcome_any_line_reads_explicit_selected_team_line(self):
        market = self._market(
            "Spread: Indiana (-21.5)",
            "Indiana",
            "Northwestern",
            "nw-215",
            line=-21.5,
        )
        pick = _pick(
            selection="NORTHWESTERN 21",
            team_hint="Northwestern",
            event_hints=["Northwestern"],
            bet_types=["spread"],
            spread_lines=["+21"],
        )
        selected = capper._spread_outcome_any_line(market, pick)
        self.assertIsNotNone(selected)
        self.assertEqual(selected[0], "Northwestern")
        self.assertEqual(selected[1].token_id, "nw-215-no")
        self.assertEqual(str(selected[2]), "21.5")

    def test_find_spread_alternatives_prefers_better_equidistant_line(self):
        event = SimpleNamespace(
            id="nw-ind",
            slug="cfb-nw-ind-2026-09-25",
            title="Northwestern vs Indiana",
            start_time="2020-01-01T00:00:00+00:00",
            markets=[
                self._market(
                    "Spread: Indiana (-20.5)",
                    "Indiana",
                    "Northwestern",
                    "nw-205",
                    line=-20.5,
                ),
                self._market(
                    "Spread: Indiana (-21.5)",
                    "Indiana",
                    "Northwestern",
                    "nw-215",
                    line=-21.5,
                ),
                self._market(
                    "2H Spread: Indiana (-21.5)",
                    "Indiana",
                    "Northwestern",
                    "nw-2h-215",
                    line=-21.5,
                    market_type="second_half_spreads",
                ),
            ],
        )

        class _Result:
            items = [event]

        class _Client:
            def __enter__(self): return self
            def __exit__(self, exc_type, exc, tb): return False
            def list_events(self, **kwargs): return SimpleNamespace(first_page=lambda: _Result())

        pick = _pick(
            selection="NORTHWESTERN 21",
            posted_at="2026-09-25T22:32:01+00:00",
            team_hint="Northwestern",
            event_hints=["Northwestern"],
            bet_types=["spread"],
            spread_lines=["+21"],
        )
        quotes = {
            "nw-205-no": {
                "current_buy_price": "0.50", "best_ask": "0.51", "max_price": "0.51",
                "spread": "0.02", "live_odds_american": "+96", "quote_updated_at": "now",
            },
            "nw-215-no": {
                "current_buy_price": "0.52", "best_ask": "0.53", "max_price": "0.53",
                "spread": "0.02", "live_odds_american": "-113", "quote_updated_at": "now",
            },
        }
        with (
            patch.object(capper, "PublicClient", return_value=_Client()),
            patch.object(capper, "_read_live_buy_quote", side_effect=lambda asset: quotes[asset]),
        ):
            alternatives = capper._find_spread_alternatives(pick)

        self.assertEqual([x["spread_line"] for x in alternatives], ["+21.5", "+20.5"])
        self.assertEqual(alternatives[0]["relative_to_original"], "BETTER")
        self.assertEqual(alternatives[1]["relative_to_original"], "WORSE")
        self.assertEqual(alternatives[0]["event_phase"], "LIVE")

    def test_exact_spread_selects_complementary_team_outcome(self):
        market = self._market(
            "Spread: Indiana (-21.5)",
            "Indiana",
            "Northwestern",
            "nw-215",
            line=-21.5,
        )
        pick = _pick(
            selection="NORTHWESTERN +21.5",
            team_hint="Northwestern",
            event_hints=["Northwestern"],
            bet_types=["spread"],
            spread_lines=["+21.5"],
        )
        selected = capper._select_outcome(market, pick, "spread")
        self.assertIsNotNone(selected)
        self.assertEqual(selected[0], "Northwestern")
        self.assertEqual(selected[1].token_id, "nw-215-no")

    def test_period_spread_type_is_not_full_game(self):
        self.assertTrue(capper._full_game_market_type_matches("spreads", "spread"))
        self.assertFalse(
            capper._full_game_market_type_matches("second_half_spreads", "spread")
        )
        self.assertFalse(capper._full_game_market_type_matches("q3_spreads", "spread"))

    def test_manual_buy_can_use_explicit_alternate_but_keeps_original_pick_id(self):
        saved = {
            "alternative_id": "alt-1",
            "match_status": "ALTERNATE",
            "market_type": "spread",
            "event_slug": "cfb-nw-ind-2026-09-25",
            "event_title": "Northwestern vs Indiana",
            "event_start_at": "2020-01-01T00:00:00+00:00",
            "market_accepting_orders": True,
            "market": "Northwestern +21.5",
            "market_url": "https://polymarket.com/sports/cfb/cfb-nw-ind-2026-09-25",
            "outcome": "Northwestern",
            "asset_id": "nw-215",
            "spread_line": "+21.5",
        }

        class _Book:
            asks = [SimpleNamespace(price="0.52")]

        class _Client:
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def get_price(self, **kwargs):
                return "0.51"
            def get_spread(self, **kwargs):
                return "0.02"
            def get_order_book(self, **kwargs):
                return _Book()

        core = SimpleNamespace(
            MAX_AUTO_TRADE_USDC=Decimal("50"),
            MAX_DAILY_BUDGET_USDC=Decimal("100"),
            MAX_PRICE=Decimal("0.95"),
            MAX_SPREAD=Decimal("0.08"),
            EXECUTIONS_FILE=None,
            _daily_budget_used=lambda: Decimal("0"),
        )
        calls = []
        class _Remote:
            def _queue_load(self): return {}
            def _expire_stale_buys_persisted(self): return []
            def _enqueue(self, action, payload):
                calls.append((action, payload))
                return {"id": "alt-buy"}

        pick = _pick(
            selection="Northwestern +21.5",
            team_hint="Northwestern",
            event_hints=["Northwestern"],
            bet_types=["spread"],
            spread_lines=["+21.5"],
        )
        with (
            patch.object(capper.live_control, "_executor_ready", return_value=(True, {})),
            patch.object(capper, "PublicClient", return_value=_Client()),
            patch.object(capper.nfl, "_pending_auto_budget", return_value=Decimal("0")),
        ):
            result = capper._prepare_manual_buy(
                pick,
                core=core,
                remote=_Remote(),
                unit_usdc=Decimal("10"),
                matched=saved,
                strategy_pick_id="original-northwestern-signal",
                strategy_selection="NORTHWESTERN 21",
                strategy_alternate_line="+21.5",
            )

        self.assertEqual(result["best_ask"], "0.52")
        self.assertEqual(calls[0][0], "BUY")
        self.assertEqual(calls[0][1]["strategy_pick_id"], "original-northwestern-signal")
        self.assertEqual(calls[0][1]["strategy_selection"], "NORTHWESTERN 21")
        self.assertEqual(calls[0][1]["strategy_alternate_line"], "+21.5")
        self.assertEqual(calls[0][1]["asset_id"], "nw-215")


class CfbPreviewSafetyTests(unittest.TestCase):
    def test_prepare_preview_enqueues_preview_never_buy(self):
        outcome = SimpleNamespace(label="Under", token_id="under-token")
        market = SimpleNamespace(question="Texas vs Tennessee: O/U 52.5")
        event = SimpleNamespace(slug="cfb-tx-tenn-2026-09-26", title="Texas vs Tennessee")

        class _Book:
            asks = [SimpleNamespace(price="0.43")]

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get_price(self, **kwargs):
                return "0.42"

            def get_spread(self, **kwargs):
                return "0.02"

            def get_order_book(self, **kwargs):
                return _Book()

        core = SimpleNamespace(
            MAX_AUTO_TRADE_USDC=Decimal("50"),
            MAX_DAILY_BUDGET_USDC=Decimal("100"),
            MAX_PRICE=Decimal("0.95"),
            MAX_SPREAD=Decimal("0.08"),
            _daily_budget_used=lambda: Decimal("0"),
            auto_trading_enabled=lambda: True,
        )
        calls = []

        class _Remote:
            def _queue_load(self):
                return {}

            def _enqueue(self, action, payload):
                calls.append((action, payload))
                return {"id": "cfb-preview-1"}

        pick = _pick(
            selection="UNDER 52.5",
            bet_types=["total"],
            team_hint=None,
            event_hints=["Texas", "Tennessee"],
            spread_lines=[],
            total_side="UNDER",
            total_line=52.5,
        )

        with (
            patch.object(capper.live_control, "_executor_ready", return_value=(True, {})),
            patch.object(capper, "_find_market", return_value=(event, market, "Under", outcome)),
            patch.object(capper, "PublicClient", return_value=_Client()),
            patch.object(capper.nfl, "_pending_auto_budget", return_value=Decimal("0")),
        ):
            result = capper._prepare_preview(
                pick,
                core=core,
                remote=_Remote(),
                unit_usdc=Decimal("10"),
            )

        self.assertEqual(result["status"], "PREVIEW_QUEUED")
        self.assertEqual(result["stake_usdc"], "15.09")
        self.assertEqual(result["target_profit_usdc"], "20.00")
        self.assertEqual(result["sizing_mode"], "TO_WIN")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "PREVIEW")
        self.assertEqual(calls[0][1]["market_type"], "total")
        self.assertEqual(calls[0][1]["budget_usdc"], "15.09")
        self.assertEqual(calls[0][1]["strategy_target_profit_usdc"], "20.00")
        self.assertEqual(calls[0][1]["strategy_sizing_mode"], "TO_WIN")
        self.assertTrue(calls[0][1]["trade_id"].startswith("cfb-capper-"))
        self.assertFalse(calls[0][1]["auto"])


    def test_prepare_manual_buy_queues_buy_without_auto_trading(self):
        outcome = SimpleNamespace(label="Baylor", token_id="baylor-token")
        market = SimpleNamespace(question="Baylor vs TCU")
        event = SimpleNamespace(slug="cfb-baylor-tcu-2026-09-26", title="Baylor vs TCU")

        class _Book:
            asks = [SimpleNamespace(price="0.47")]

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get_price(self, **kwargs):
                return "0.46"

            def get_spread(self, **kwargs):
                return "0.02"

            def get_order_book(self, **kwargs):
                return _Book()

        core = SimpleNamespace(
            MAX_AUTO_TRADE_USDC=Decimal("50"),
            MAX_DAILY_BUDGET_USDC=Decimal("100"),
            MAX_PRICE=Decimal("0.95"),
            MAX_SPREAD=Decimal("0.08"),
            EXECUTIONS_FILE=None,
            _daily_budget_used=lambda: Decimal("0"),
            auto_trading_enabled=lambda: False,
        )
        calls = []

        class _Remote:
            def _queue_load(self):
                return {}

            def _expire_stale_buys_persisted(self):
                return []

            def _enqueue(self, action, payload):
                calls.append((action, payload))
                return {"id": "cfb-buy-1"}

        pick = _pick(
            selection="BAYLOR +7",
            team_hint="Baylor",
            event_hints=["Baylor", "TCU"],
            bet_types=["spread"],
            spread_lines=["+7"],
        )

        with (
            patch.object(capper.live_control, "_executor_ready", return_value=(True, {})),
            patch.object(capper, "_find_market", return_value=(event, market, "Baylor", outcome)),
            patch.object(capper, "PublicClient", return_value=_Client()),
            patch.object(capper.nfl, "_pending_auto_budget", return_value=Decimal("0")),
        ):
            result = capper._prepare_manual_buy(
                pick,
                core=core,
                remote=_Remote(),
                unit_usdc=Decimal("10"),
            )

        self.assertEqual(result["status"], "MANUAL_BUY_QUEUED")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "BUY")
        self.assertFalse(calls[0][1]["auto"])
        self.assertTrue(calls[0][1]["manual"])
        self.assertEqual(calls[0][1]["source"], "termux_executor")
        self.assertEqual(calls[0][1]["strategy_execution_mode"], "manual")
        self.assertEqual(calls[0][1]["asset_id"], "baylor-token")
        self.assertEqual(calls[0][1]["max_price"], "0.47")
        self.assertEqual(calls[0][1]["budget_usdc"], "17.74")
        self.assertEqual(calls[0][1]["strategy_target_profit_usdc"], "20.00")
        self.assertEqual(calls[0][1]["strategy_sizing_mode"], "TO_WIN")

    def test_manual_buy_uses_saved_match_without_re_resolving_event(self):
        saved = {
            "match_status": "MATCHED",
            "market_type": "spread",
            "event_slug": "cfb-baylor-tcu-2026-09-26",
            "event_title": "Baylor vs TCU",
            "event_start_at": "2020-01-01T00:00:00+00:00",
            "market_accepting_orders": True,
            "market": "Baylor +7",
            "market_url": "https://polymarket.com/sports/cfb/cfb-baylor-tcu-2026-09-26",
            "outcome": "Baylor",
            "asset_id": "saved-baylor-token",
        }

        class _Book:
            asks = [SimpleNamespace(price="0.47")]

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get_price(self, **kwargs):
                self.asset_id = kwargs["asset_id"]
                return "0.46"

            def get_spread(self, **kwargs):
                return "0.02"

            def get_order_book(self, **kwargs):
                return _Book()

        core = SimpleNamespace(
            MAX_AUTO_TRADE_USDC=Decimal("50"),
            MAX_DAILY_BUDGET_USDC=Decimal("100"),
            MAX_PRICE=Decimal("0.95"),
            MAX_SPREAD=Decimal("0.08"),
            EXECUTIONS_FILE=None,
            _daily_budget_used=lambda: Decimal("0"),
        )
        calls = []

        class _Remote:
            def _queue_load(self):
                return {}

            def _expire_stale_buys_persisted(self):
                return []

            def _enqueue(self, action, payload):
                calls.append((action, payload))
                return {"id": "saved-match-buy"}

        pick = _pick(
            selection="BAYLOR +7",
            team_hint="Baylor",
            event_hints=["Baylor"],
            bet_types=["spread"],
            spread_lines=["+7"],
        )

        with (
            patch.object(capper.live_control, "_executor_ready", return_value=(True, {})),
            patch.object(capper, "_find_market", side_effect=AssertionError("must not re-resolve")),
            patch.object(capper, "PublicClient", return_value=_Client()),
            patch.object(capper.nfl, "_pending_auto_budget", return_value=Decimal("0")),
        ):
            result = capper._prepare_manual_buy(
                pick,
                core=core,
                remote=_Remote(),
                unit_usdc=Decimal("10"),
                matched=saved,
            )

        self.assertEqual(result["asset_id"], "saved-baylor-token")
        self.assertEqual(calls[0][1]["asset_id"], "saved-baylor-token")
        self.assertEqual(calls[0][1]["market_url"], saved["market_url"])
        self.assertEqual(calls[0][1]["strategy_event_phase"], "LIVE")

    def test_live_event_phase_remains_actionable_while_market_open(self):
        saved = {
            "status": "MATCHED_LIVE",
            "match_status": "MATCHED",
            "market_type": "spread",
            "event_slug": "cfb-baylor-tcu-2026-09-26",
            "event_title": "Baylor vs TCU",
            "event_start_at": "2020-01-01T00:00:00+00:00",
            "market_accepting_orders": True,
            "market_url": "https://polymarket.com/sports/cfb/cfb-baylor-tcu-2026-09-26",
            "outcome": "Baylor",
            "asset_id": "saved-baylor-token",
        }
        item = capper._status_pick_item(saved)
        self.assertEqual(item["event_phase"], "LIVE")
        self.assertTrue(item["buy_available"])

    def test_closed_market_is_not_actionable(self):
        saved = {
            "status": "EVENT_CLOSED",
            "match_status": "MATCHED",
            "market_type": "spread",
            "event_slug": "cfb-baylor-tcu-2026-09-26",
            "event_title": "Baylor vs TCU",
            "event_start_at": "2020-01-01T00:00:00+00:00",
            "market_accepting_orders": False,
            "market_url": "https://polymarket.com/sports/cfb/cfb-baylor-tcu-2026-09-26",
            "outcome": "Baylor",
            "asset_id": "saved-baylor-token",
        }
        item = capper._status_pick_item(saved)
        self.assertEqual(item["event_phase"], "CLOSED")
        self.assertFalse(item["buy_available"])

    def test_status_buy_available_only_after_polymarket_match(self):
        unmatched = capper._status_pick_item(
            {
                "id": "unmatched",
                "status": "MATCHED_PREGAME",
                "selection": "CLEMSON ML",
            }
        )
        self.assertFalse(unmatched["buy_available"])

        matched = capper._status_pick_item(
            {
                "id": "matched",
                "status": "MATCHED_PREGAME",
                "selection": "CLEMSON ML",
                "match_status": "MATCHED",
                "market_type": "moneyline",
                "market_url": "https://polymarket.com/sports/cfb/cfb-clemson-unc-2026-09-26",
                "outcome": "Clemson",
                "asset_id": "clemson-token",
                "market": "Clemson vs UNC",
            }
        )
        self.assertTrue(matched["buy_available"])
        self.assertEqual(matched["market"], "Clemson vs UNC")

    def test_prepare_preview_respects_global_auto_trading_gate(self):
        core = SimpleNamespace(
            auto_trading_enabled=lambda: False,
        )
        pick = _pick(
            selection="TEXAS ML",
            bet_types=["moneyline"],
            team_hint="Texas",
            event_hints=["Texas", "Tennessee"],
            spread_lines=[],
        )
        with self.assertRaisesRegex(RuntimeError, "AUTO_TRADING is disabled"):
            capper._prepare_preview(
                pick,
                core=core,
                remote=SimpleNamespace(),
                unit_usdc=Decimal("10"),
            )


class CfbScoreboardFallbackTests(unittest.TestCase):
    @staticmethod
    def _response(team_a, score_a, team_b, score_b, event_id="game-1"):
        payload = {
            "events": [
                {
                    "id": event_id,
                    "name": f"{team_a} vs {team_b}",
                    "competitions": [
                        {
                            "status": {"type": {"completed": True}},
                            "competitors": [
                                {
                                    "score": str(score_a),
                                    "team": {
                                        "displayName": f"{team_a} Team",
                                        "shortDisplayName": team_a,
                                        "name": team_a,
                                        "location": team_a,
                                        "abbreviation": team_a[:4].upper(),
                                    },
                                },
                                {
                                    "score": str(score_b),
                                    "team": {
                                        "displayName": f"{team_b} Team",
                                        "shortDisplayName": team_b,
                                        "name": team_b,
                                        "location": team_b,
                                        "abbreviation": team_b[:4].upper(),
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

        return _Response()

    def test_northwestern_plus_21_grades_win_from_final_score(self):
        pick = _pick(
            posted_at="2026-09-25T22:32:01+00:00",
            selection="NORTHWESTERN 21",
            team_hint="Northwestern",
            event_hints=["Northwestern"],
            spread_lines=["+21"],
        )
        with patch.object(
            capper.httpx,
            "get",
            return_value=self._response("Northwestern", 23, "Indiana", 29),
        ):
            result = capper._scoreboard_result_for_pick(pick)

        self.assertEqual(result["pick_result"], "WIN")
        self.assertIn("Northwestern 23", result["final_score"])
        self.assertEqual(result["result_source"], "espn_final_score")

    def test_army_moneyline_grades_win_from_final_score(self):
        pick = _pick(
            source="The Syndicate",
            source_key="syndicate",
            posted_at="2026-09-25T15:39:48+00:00",
            selection="Army To Win",
            team_hint="Army",
            event_hints=["Army"],
            bet_types=["moneyline"],
            spread_lines=[],
            units=2,
        )
        with patch.object(
            capper.httpx,
            "get",
            return_value=self._response("Army", 21, "Temple", 17),
        ):
            result = capper._scoreboard_result_for_pick(pick)

        self.assertEqual(result["pick_result"], "WIN")

    def test_temple_plus_3_5_grades_loss_at_21_17(self):
        pick = _pick(
            posted_at="2026-09-25T19:46:30+00:00",
            selection="TEMPLE +3.5",
            team_hint="Temple",
            event_hints=["Temple"],
            spread_lines=["+3.5"],
        )
        with patch.object(
            capper.httpx,
            "get",
            return_value=self._response("Army", 21, "Temple", 17),
        ):
            result = capper._scoreboard_result_for_pick(pick)

        self.assertEqual(result["pick_result"], "LOSS")

    def test_matched_early_pick_uses_event_start_date_to_close(self):
        pick = _pick(
            posted_at="2026-09-23T16:00:00+00:00",
            selection="OREGON -3",
            team_hint="Oregon",
            event_hints=["Oregon"],
            spread_lines=["-3"],
        )
        record = {
            "id": "oregon-early",
            "status": "MATCHED_LIVE",
            "match_status": "MATCHED",
            "market_type": "spread",
            "event_slug": "cfb-ore-usc-2026-09-26",
            "event_title": "Oregon vs USC",
            "event_start_at": "2026-09-26T23:30:00+00:00",
            "event_start_checked_at": "2026-09-26T23:00:00+00:00",
            "market_accepting_orders": True,
            "market_url": "https://polymarket.com/sports/cfb/cfb-ore-usc-2026-09-26",
            "outcome": "Oregon",
            "asset_id": "oregon-token",
            "pick": pick,
        }
        final_event = self._response(
            "Oregon",
            41,
            "USC",
            27,
            event_id="oregon-usc-final",
        ).json()["events"][0]
        queried_days = []

        def scoreboard_events(game_day):
            queried_days.append(game_day)
            return [final_event] if game_day == "20260926" else []

        refreshed = {
            "event_title": "Oregon vs USC",
            "event_start_at": "2026-09-26T23:30:00+00:00",
            "event_start_checked_at": "2026-09-27T04:00:00+00:00",
            "event_finished_at": None,
            "event_closed": False,
            "event_ended": False,
            "event_live": True,
            "game_status": "Live",
            "market_accepting_orders": True,
        }
        with (
            patch.object(capper, "_refresh_saved_event_state", return_value=refreshed),
            patch.object(capper, "_espn_scoreboard_events", side_effect=scoreboard_events),
            patch.object(
                capper,
                "_read_live_buy_quote",
                side_effect=AssertionError("finished ESPN game must not stay live"),
            ),
        ):
            changed = capper._refresh_record_runtime(record)

        self.assertTrue(changed)
        self.assertEqual(record["status"], "EVENT_CLOSED")
        self.assertEqual(record["event_phase"], "CLOSED")
        self.assertEqual(record["espn_phase"], "CLOSED")
        self.assertEqual(record["pick_result"], "WIN")
        self.assertEqual(record["espn_event_id"], "oregon-usc-final")
        self.assertEqual(queried_days[0], "20260926")
        self.assertNotIn("20260923", queried_days)

    def test_stored_espn_event_id_disambiguates_same_team_hint(self):
        pick = _pick(
            posted_at="2026-09-26T12:00:00+00:00",
            selection="TEXAS +3.5",
            team_hint="Texas",
            event_hints=["Texas"],
            spread_lines=["+3.5"],
        )
        wrong = self._response(
            "Texas Tech",
            49,
            "Baylor",
            14,
            event_id="wrong-game",
        ).json()["events"][0]
        target = self._response(
            "Texas",
            20,
            "Tennessee",
            17,
            event_id="target-game",
        ).json()["events"][0]

        with patch.object(
            capper,
            "_espn_scoreboard_events",
            side_effect=lambda game_day: [wrong, target] if game_day == "20260926" else [],
        ):
            snapshot = capper._scoreboard_snapshot_for_pick(
                pick,
                event_start_at="2026-09-26T16:00:00+00:00",
                espn_event_id="target-game",
            )

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["event_id"], "target-game")
        self.assertEqual(snapshot["phase"], "CLOSED")
        self.assertIn("Texas 20", snapshot["score"])

    def test_unresolved_closed_signal_is_regraded_without_saved_market(self):
        record = {
            "id": "northwestern-closed",
            "status": "EVENT_CLOSED",
            "selection": "NORTHWESTERN 21",
            "pick": _pick(
                posted_at="2026-09-25T22:32:01+00:00",
                selection="NORTHWESTERN 21",
                team_hint="Northwestern",
                event_hints=["Northwestern"],
                spread_lines=["+21"],
            ),
        }
        resolved = {
            "pick_result": "WIN",
            "result_source": "espn_final_score",
            "final_score": "Northwestern 23 - Indiana 29",
        }

        with (
            patch.object(capper, "_scoreboard_snapshot_for_pick", return_value=None),
            patch.object(capper, "_scoreboard_result_for_pick", return_value=resolved),
        ):
            changed = capper._refresh_unresolved_closed_result(record)

        self.assertTrue(changed)
        self.assertEqual(record["pick_result"], "WIN")
        self.assertEqual(record["final_score"], "Northwestern 23 - Indiana 29")

    def test_open_execution_exposes_sell_button_state(self):
        record = {
            "id": "cfb-signal-open",
            "status": "MATCHED_LIVE",
            "selection": "CLEMSON ML",
        }
        executions = {
            "trade-open": {
                "id": "trade-open",
                "strategy_pick_id": "cfb-signal-open",
                "status": "ORDER_SUBMITTED",
                "budget_usdc": "10",
            }
        }

        item = capper._status_pick_item(record, executions)

        self.assertEqual(item["trade_id"], "trade-open")
        self.assertTrue(item["sell_available"])


    def test_unmatched_finished_total_is_graded_without_polymarket_match(self):
        pick = _pick(
            posted_at="2026-09-26T15:00:00+00:00",
            selection="TEXAS/ TENNESSEE UNDER 55",
            team_hint=None,
            event_hints=[],
            bet_types=["total"],
            spread_lines=[],
            total_side="UNDER",
            total_line=55,
        )
        record = {
            "id": "texas-tennessee-under",
            "status": "RETRYING",
            "selection": pick["selection"],
            "pick": pick,
        }

        with patch.object(
            capper.httpx,
            "get",
            return_value=self._response("Texas", 20, "Tennessee", 17),
        ):
            changed = capper._refresh_unmatched_record(record)

        self.assertTrue(changed)
        self.assertEqual(record["status"], "EVENT_CLOSED")
        self.assertEqual(record["match_status"], "SCOREBOARD_ONLY")
        self.assertEqual(record["pick_result"], "WIN")
        self.assertIn("Texas 20", record["final_score"])
        self.assertEqual(record["pick"]["event_hints"], ["TEXAS", "TENNESSEE"])

    def test_unmatched_live_spread_retries_and_exposes_alternate(self):
        pick = _pick(
            posted_at="2026-09-26T22:00:00+00:00",
            selection="SOUTH ALABAMA +21",
            team_hint="South Alabama",
            event_hints=["South Alabama"],
            spread_lines=["+21"],
        )
        record = {
            "id": "south-alabama",
            "status": "RETRYING",
            "selection": pick["selection"],
            "pick": pick,
        }
        live = {
            "event_id": "live-1",
            "event_title": "South Alabama vs Kentucky",
            "phase": "LIVE",
            "status": "Q3 06:21",
            "score": "South Alabama 17 - Kentucky 21",
            "_competitors": [],
            "_scores": [],
        }
        alternate = {
            "alternative_id": "alt-205",
            "event_title": "South Alabama vs Kentucky",
            "event_start_at": "2026-09-26T23:00:00+00:00",
            "event_phase": "LIVE",
            "market_url": "https://polymarket.com/sports/cfb/cfb-south-alabama-kentucky-2026-09-26",
            "spread_line": "+20.5",
            "best_ask": "0.51",
            "live_odds_american": "-104",
            "relative_to_original": "WORSE",
        }

        with (
            patch.object(capper, "_scoreboard_snapshot_for_pick", return_value=live),
            patch.object(capper, "_resolve_market_match", side_effect=ValueError("exact unavailable")),
            patch.object(capper, "_find_spread_alternatives", return_value=[alternate]),
        ):
            changed = capper._refresh_unmatched_record(record)

        self.assertTrue(changed)
        self.assertEqual(record["status"], "MATCHED_LIVE_ALTERNATE")
        self.assertEqual(record["match_status"], "ALTERNATE_AVAILABLE")
        self.assertEqual(record["live_score"], "South Alabama 17 - Kentucky 21")
        self.assertEqual(record["live_alternatives"][0]["spread_line"], "+20.5")

    def test_live_score_snapshot_is_exposed_on_status_item(self):
        record = {
            "id": "live-cfb",
            "status": "RETRYING",
            "selection": "ARKANSAS -7",
            "event_phase": "LIVE",
            "espn_phase": "LIVE",
            "espn_score": "Arkansas 14 - Opponent 10",
            "espn_status": "Q2 03:10",
            "live_score": "Arkansas 14 - Opponent 10",
        }
        item = capper._status_pick_item(record, {})
        self.assertEqual(item["espn_score"], "Arkansas 14 - Opponent 10")
        self.assertEqual(item["espn_status"], "Q2 03:10")
        self.assertEqual(item["live_score"], "Arkansas 14 - Opponent 10")


class CfbFinishedResultsAndPerformanceTests(unittest.TestCase):
    def test_resolved_signal_outcome_uses_authoritative_token_resolution(self):
        yes = SimpleNamespace(label="Clemson", token_id="clemson-token", price="1")
        no = SimpleNamespace(label="Opponent", token_id="opp-token", price="0")
        market = SimpleNamespace(
            id="market-1",
            slug="cfb-clemson-opponent-2026-09-26",
            state=SimpleNamespace(closed=True),
            resolution=SimpleNamespace(uma_resolution_status="resolved"),
            outcomes=SimpleNamespace(yes=yes, no=no),
        )

        class _Markets:
            def iter_items(self):
                return iter([market])

        class _Client:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def list_markets(self, **kwargs):
                self.kwargs = kwargs
                return _Markets()

        with patch.object(capper, "PublicClient", return_value=_Client()):
            result = capper._resolved_signal_outcome("clemson-token")

        self.assertEqual(result["pick_result"], "WIN")
        self.assertEqual(result["settlement_terminal_price"], "1")
        self.assertEqual(result["settlement_market_slug"], "cfb-clemson-opponent-2026-09-26")

    def test_closed_status_item_includes_actual_trade_result_and_pnl(self):
        record = {
            "id": "cfb-signal-1",
            "status": "EVENT_CLOSED",
            "selection": "CLEMSON ML -135",
            "match_status": "MATCHED",
            "market_type": "moneyline",
            "market_url": "https://polymarket.com/sports/cfb/cfb-clemson-opponent-2026-09-26",
            "event_slug": "cfb-clemson-opponent-2026-09-26",
            "event_closed": True,
            "outcome": "Clemson",
            "asset_id": "clemson-token",
            "pick_result": "WIN",
        }
        executions = {
            "trade-1": {
                "id": "trade-1",
                "strategy_pick_id": "cfb-signal-1",
                "strategy_source": "Slam - CFB",
                "strategy_sport": "CFB",
                "status": "SETTLED_WIN",
                "actual_cost_usdc": "10",
                "realized_pnl": "7.50",
                "settlement": {"result": "WIN"},
            }
        }

        item = capper._status_pick_item(record, executions)

        self.assertEqual(item["event_phase"], "CLOSED")
        self.assertEqual(item["pick_result"], "WIN")
        self.assertTrue(item["trade_executed"])
        self.assertEqual(item["trade_result"], "WIN")
        self.assertEqual(item["trade_pnl_usdc"], "7.50")
        self.assertEqual(item["trade_stake_usdc"], "10")

    def test_closed_untraded_signal_keeps_result_without_fabricated_pnl(self):
        record = {
            "id": "cfb-signal-2",
            "status": "EVENT_CLOSED",
            "selection": "TEMPLE +3.5",
            "match_status": "MATCHED",
            "market_type": "spread",
            "market_url": "https://polymarket.com/sports/cfb/cfb-temple-opponent-2026-09-26",
            "event_slug": "cfb-temple-opponent-2026-09-26",
            "event_closed": True,
            "outcome": "Temple",
            "asset_id": "temple-token",
            "pick_result": "LOSS",
        }

        item = capper._status_pick_item(record, {})

        self.assertEqual(item["pick_result"], "LOSS")
        self.assertFalse(item["trade_executed"])
        self.assertIsNone(item["trade_pnl_usdc"])

    def test_dashboard_renders_performance_and_finished_result_styles(self):
        html = '<style></style>\n  <div class="tabs"></div>\n<script></script>'
        rendered = capper._inject_dashboard_panel(html)

        self.assertIn("cfb-capper-performance", rendered)
        self.assertIn("CFB capper auto-trading", rendered)
        self.assertIn("ENABLED · AUTO LIVE", rendered)
        self.assertIn("ENABLED · AUTO OFF", rendered)
        self.assertIn("Trade P/L", rendered)
        self.assertIn("Realized P/L", rendered)
        self.assertIn("7D P/L", rendered)
        self.assertIn("30D P/L", rendered)
        self.assertIn("Live P/L", rendered)
        self.assertIn("total_live_pnl_usdc", rendered)
        self.assertIn("Open positions", rendered)
        self.assertIn("cfb-open-position", rendered)
        self.assertIn("#f59e0b", rendered)
        self.assertIn("Missed P/L", rendered)
        self.assertIn("label:'WIN'", rendered)
        self.assertIn("label:'LOSS'", rendered)
        self.assertIn("NOT TRADED", rendered)
        self.assertIn("cfbFinishedToggle", rendered)
        self.assertIn("Hide finished", rendered)
        self.assertIn("SELL POSITION", rendered)
        self.assertIn("cfbSellPosition", rendered)
        self.assertIn("ESPN ", rendered)


class CfbRecoveryWindowTests(unittest.TestCase):
    def test_only_delayed_pregame_live_enabled_pick_can_recover(self):
        self.assertTrue(capper._recovery_pregame_allowed(3600, 180, 86400, "PREGAME", True))
        self.assertFalse(capper._recovery_pregame_allowed(120, 180, 86400, "PREGAME", True))
        self.assertFalse(capper._recovery_pregame_allowed(3600, 180, 86400, "LIVE", True))
        self.assertFalse(capper._recovery_pregame_allowed(3600, 180, 86400, "CLOSED", True))
        self.assertFalse(capper._recovery_pregame_allowed(3600, 180, 86400, "PREGAME", False))
        self.assertFalse(capper._recovery_pregame_allowed(90000, 180, 86400, "PREGAME", True))


class CfbNoFillHelperTests(unittest.TestCase):
    def test_cfb_uses_shared_no_fill_detection(self):
        self.assertTrue(
            capper.nfl._queue_result_is_no_fill(
                {
                    "action": "BUY",
                    "result": {
                        "ok": False,
                        "filled_shares": "0",
                        "status": "TEST_BUY_UNFILLED_CANCELED",
                    },
                }
            )
        )



class CfbLegacyRecordReconciliationTests(unittest.TestCase):
    @staticmethod
    def _response(team_a, score_a, team_b, score_b, *, completed=True, state=None, detail=""):
        status_type = {"completed": completed}
        if state is not None:
            status_type["state"] = state
        if detail:
            status_type["shortDetail"] = detail
        payload = {
            "events": [
                {
                    "id": "legacy-game",
                    "name": f"{team_a} vs {team_b}",
                    "competitions": [
                        {
                            "status": {"type": status_type},
                            "competitors": [
                                {
                                    "score": str(score_a),
                                    "team": {
                                        "displayName": team_a,
                                        "shortDisplayName": team_a,
                                        "name": team_a,
                                        "location": team_a,
                                        "abbreviation": team_a[:4].upper(),
                                    },
                                },
                                {
                                    "score": str(score_b),
                                    "team": {
                                        "displayName": team_b,
                                        "shortDisplayName": team_b,
                                        "name": team_b,
                                        "location": team_b,
                                        "abbreviation": team_b[:4].upper(),
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

        return _Response()

    def test_missing_pick_spread_is_reconstructed_and_finished_game_is_graded(self):
        record = {
            "id": "legacy-oregon",
            "source": "Slam - CFB",
            "telegram_source": "SLAM - All Access",
            "posted_at": "2026-09-26T22:15:00+00:00",
            "selection": "OREGON -3 MAX",
            "units": "5",
            "status": "RETRYING",
        }

        with patch.object(
            capper.httpx,
            "get",
            return_value=self._response("Oregon", 41, "USC", 27),
        ):
            changed = capper._refresh_unmatched_record(record)

        self.assertTrue(changed)
        self.assertEqual(record["status"], "EVENT_CLOSED")
        self.assertEqual(record["pick_result"], "WIN")
        self.assertEqual(record["event_phase"], "CLOSED")
        self.assertEqual(record["pick"]["team_hint"], "OREGON")
        self.assertEqual(record["pick"]["spread_lines"], ["-3"])

    def test_missing_pick_finished_loss_is_graded_from_original_line(self):
        record = {
            "id": "legacy-south-carolina",
            "source": "Slam - CFB",
            "telegram_source": "SLAM - All Access",
            "posted_at": "2026-09-26T22:15:00+00:00",
            "selection": "SOUTH CAROLINA +13",
            "units": "2",
            "status": "RETRYING",
        }

        with patch.object(
            capper.httpx,
            "get",
            return_value=self._response("South Carolina", 18, "Alabama", 49),
        ):
            capper._refresh_unmatched_record(record)

        self.assertEqual(record["status"], "EVENT_CLOSED")
        self.assertEqual(record["pick_result"], "LOSS")
        self.assertEqual(record["final_score"], "South Carolina 18 - Alabama 49")

    def test_pending_game_is_marked_pregame_not_finished(self):
        record = {
            "id": "legacy-arkansas",
            "source": "Slam - CFB",
            "telegram_source": "SLAM - All Access",
            "posted_at": "2026-09-27T10:00:00+00:00",
            "selection": "ARKANSAS -7",
            "units": "1",
            "status": "RETRYING",
        }
        pending = {
            "event_id": "pending-game",
            "event_title": "Arkansas vs Opponent",
            "phase": "PREGAME",
            "status": "7:00 PM",
            "score": "Arkansas 0 - Opponent 0",
            "_competitors": [],
            "_scores": [],
        }

        with (
            patch.object(capper, "_scoreboard_snapshot_for_pick", return_value=pending),
            patch.object(capper, "_resolve_market_match", side_effect=ValueError("not open yet")),
            patch.object(capper, "_find_spread_alternatives", return_value=[]),
        ):
            capper._refresh_unmatched_record(record)

        self.assertEqual(record["espn_phase"], "PREGAME")
        self.assertEqual(record["status"], "RETRYING")
        self.assertIn("game is pending on ESPN", record["reason"])
        self.assertNotEqual(record.get("pick_result"), "WIN")
        self.assertNotEqual(record.get("pick_result"), "LOSS")


class CfbPersistedCleanupTests(unittest.TestCase):
    def test_old_nfl_rows_are_purged_but_explicit_college_matchup_remains(self):
        nfl_pick = _pick(
            selection="Vikings To Win",
            team_hint="Vikings",
            event_hints=["Vikings"],
            bet_types=["moneyline"],
            spread_lines=[],
        )
        cfb_pick = _pick(
            source="The Syndicate",
            source_key="syndicate",
            selection="Hawaii Rainbow Warriors v Wyoming Cowboys Under 45.5 Points",
            team_hint=None,
            event_hints=["Hawaii Rainbow Warriors", "Wyoming Cowboys"],
            bet_types=["total"],
            spread_lines=[],
            total_side="UNDER",
            total_line=45.5,
        )
        signals = {
            "old-nfl": {"selection": nfl_pick["selection"], "pick": nfl_pick},
            "real-cfb": {"selection": cfb_pick["selection"], "pick": cfb_pick},
        }

        removed = capper._purge_misrouted_nfl_signals(signals)

        self.assertEqual(removed, ["Vikings To Win"])
        self.assertNotIn("old-nfl", signals)
        self.assertIn("real-cfb", signals)

    def test_semantic_lookup_reuses_normalized_texas_tech_record(self):
        old = _pick(
            selection="TEXAS TEXCH -34.5",
            team_hint="TEXAS TEXCH",
            event_hints=["TEXAS TEXCH"],
            spread_lines=["-34.5"],
        )
        normalized = capper._normalize_legacy_bridge_pick(old)
        signals = {
            "legacy-key": {
                "selection": old["selection"],
                "pick": old,
            }
        }

        self.assertEqual(
            capper._signal_key_by_semantic(signals, normalized),
            "legacy-key",
        )


    def test_old_nfl_rows_without_pick_objects_are_purged(self):
        signals = {
            "saints": {
                "source": "Syndicate - CFB",
                "posted_at": "2026-09-26T15:40:00+00:00",
                "selection": "Saints To Win",
                "status": "RETRYING",
            },
            "vikings": {
                "source": "Syndicate - CFB",
                "posted_at": "2026-09-26T15:40:00+00:00",
                "selection": "Vikings To Win",
                "status": "RETRYING",
            },
            "steelers": {
                "source": "Syndicate - CFB",
                "posted_at": "2026-09-26T15:40:00+00:00",
                "selection": "Steelers +3.5",
                "status": "RETRYING",
            },
            "browns": {
                "source": "Syndicate - CFB",
                "posted_at": "2026-09-26T15:40:00+00:00",
                "selection": "Browns +3.5",
                "status": "RETRYING",
            },
        }

        removed = capper._purge_misrouted_nfl_signals(signals)

        self.assertEqual(signals, {})
        self.assertCountEqual(
            removed,
            ["Saints To Win", "Vikings To Win", "Steelers +3.5", "Browns +3.5"],
        )

    def test_persisted_nfl_row_with_corrupted_two_team_hints_is_still_purged(self):
        signals = {
            "vikings": {
                "source": "Syndicate - CFB",
                "posted_at": "2026-09-26T15:40:00+00:00",
                "selection": "Vikings To Win",
                "status": "RETRYING",
                "pick": _pick(
                    source="The Syndicate",
                    source_key="syndicate",
                    selection="Vikings To Win",
                    team_hint="Vikings",
                    event_hints=["Vikings", "DAL"],
                    bet_types=["moneyline"],
                    spread_lines=[],
                ),
            }
        }

        capper._purge_misrouted_nfl_signals(signals)
        self.assertEqual(signals, {})

    def test_compound_colts_nfl_row_without_pick_is_purged(self):
        signals = {
            "colts": {
                "source": "Syndicate - CFB",
                "posted_at": "2026-09-26T15:40:00+00:00",
                "selection": "Colts +7.5 + Under 51.5 Points",
                "status": "IGNORED_UNSUPPORTED",
            }
        }
        capper._purge_misrouted_nfl_signals(signals)
        self.assertEqual(signals, {})

    def test_nontraded_legacy_duplicate_is_collapsed_to_graded_record(self):
        old = {
            "id": "old-south-alabama",
            "source": "Slam - CFB",
            "telegram_source": "SLAM - All Access",
            "posted_at": "2026-09-26T15:43:48+00:00",
            "selection": "SOUTH ALABAMA +21",
            "units": "2",
            "status": "RETRYING",
        }
        graded_pick = _pick(
            posted_at="2026-09-26T16:06:13+00:00",
            selection="SOUTH ALABAMA +21",
            team_hint="SOUTH ALABAMA",
            event_hints=["SOUTH ALABAMA"],
            spread_lines=["+21"],
        )
        graded = {
            "id": "graded-south-alabama",
            "source": "Slam - CFB",
            "telegram_source": "SLAM - All Access",
            "posted_at": graded_pick["posted_at"],
            "selection": graded_pick["selection"],
            "units": "2",
            "status": "EVENT_CLOSED",
            "pick": graded_pick,
            "pick_result": "WIN",
            "espn_phase": "CLOSED",
            "final_score": "South Alabama 24 - Alabama 41",
        }
        signals = {
            "old-south-alabama": old,
            "graded-south-alabama": graded,
        }

        removed = capper._dedupe_persisted_signals(signals)

        self.assertEqual(len(signals), 1)
        self.assertIn("graded-south-alabama", signals)
        self.assertEqual(signals["graded-south-alabama"]["pick_result"], "WIN")
        self.assertEqual(removed, ["SOUTH ALABAMA +21"])

    def test_duplicate_with_trade_state_is_not_removed(self):
        pick = _pick(
            posted_at="2026-09-26T16:00:00+00:00",
            selection="AUBURN -10",
            team_hint="AUBURN",
            event_hints=["AUBURN"],
            spread_lines=["-10"],
        )
        signals = {
            "traded": {
                "id": "traded",
                "source": "Slam - CFB",
                "posted_at": pick["posted_at"],
                "selection": "AUBURN -10",
                "pick": pick,
                "trade_id": "trade-1",
                "status": "EXECUTOR_DONE",
            },
            "legacy": {
                "id": "legacy",
                "source": "Slam - CFB",
                "posted_at": pick["posted_at"],
                "selection": "34-20 TYPE GAME. AUBURN -10.",
                "status": "RETRYING",
            },
        }

        removed = capper._dedupe_persisted_signals(signals)

        self.assertEqual(removed, [])
        self.assertEqual(len(signals), 2)


class CfbLegacyBridgeNormalizationTests(unittest.TestCase):
    def test_inline_total_matchups_are_recovered(self):
        texas = capper._normalize_legacy_bridge_pick(
            _pick(
                selection="TEXAS/ TENNESSEE UNDER 55",
                team_hint=None,
                event_hints=[],
                bet_types=["total"],
                spread_lines=[],
                total_side="UNDER",
                total_line=55,
            )
        )
        self.assertEqual(texas["event_hints"], ["TEXAS", "TENNESSEE"])

        hawaii = capper._normalize_legacy_bridge_pick(
            _pick(
                selection="Hawaii Rainbow Warriors v Wyoming Cowboys Under 45.5 Points",
                team_hint=None,
                event_hints=[],
                bet_types=["total"],
                spread_lines=[],
                total_side="UNDER",
                total_line=45.5,
            )
        )
        self.assertEqual(
            hawaii["event_hints"],
            ["Hawaii Rainbow Warriors", "Wyoming Cowboys"],
        )

    def test_texas_tech_typo_is_normalized(self):
        row = capper._normalize_legacy_bridge_pick(
            _pick(
                selection="TEXAS TEXCH -34.5",
                team_hint="TEXAS TEXCH",
                event_hints=["TEXAS TEXCH"],
                spread_lines=["-34.5"],
            )
        )
        self.assertEqual(row["selection"], "TEXAS TECH -34.5")
        self.assertEqual(row["team_hint"], "TEXAS TECH")
        self.assertEqual(row["event_hints"], ["TEXAS TECH"])

    def test_score_commentary_duplicate_is_deduped(self):
        rows = capper._normalize_bridge_picks(
            [
                _pick(selection="AUBURN -10", team_hint="AUBURN", spread_lines=["-10"]),
                _pick(
                    selection="34-20 TYPE GAME. AUBURN -10.",
                    team_hint="34-20 TYPE GAME. AUBURN",
                    spread_lines=["-10"],
                ),
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["selection"], "AUBURN -10")

    def test_bare_nfl_nickname_leak_is_blocked(self):
        for name in ("Browns", "Steelers", "Vikings", "Saints"):
            row = capper._normalize_legacy_bridge_pick(
                _pick(
                    selection=f"{name} +3.5",
                    team_hint=name,
                    event_hints=[name],
                    spread_lines=["+3.5"],
                )
            )
            self.assertIsNone(row)

    def test_bare_nfl_nickname_without_team_hint_is_blocked(self):
        row = capper._normalize_legacy_bridge_pick(
            _pick(
                selection="Saints To Win",
                team_hint=None,
                event_hints=[],
                bet_types=["moneyline"],
                spread_lines=[],
            )
        )
        self.assertIsNone(row)

    def test_bare_nfl_nickname_with_corrupt_two_team_hints_is_blocked(self):
        row = capper._normalize_legacy_bridge_pick(
            _pick(
                selection="Vikings To Win",
                team_hint="Vikings",
                event_hints=["Vikings", "DAL"],
                bet_types=["moneyline"],
                spread_lines=[],
            )
        )
        self.assertIsNone(row)

    def test_explicit_college_cowboys_matchup_is_not_blocked(self):
        row = capper._normalize_legacy_bridge_pick(
            _pick(
                selection="Hawaii Rainbow Warriors v Wyoming Cowboys Under 45.5 Points",
                team_hint=None,
                event_hints=[],
                bet_types=["total"],
                spread_lines=[],
                total_side="UNDER",
                total_line=45.5,
            )
        )
        self.assertIsNotNone(row)
        self.assertEqual(len(row["event_hints"]), 2)


class CfbDashboardHistoryFilterTests(unittest.TestCase):
    def test_cfb_uses_shared_last_24h_filter_for_signals_and_settled_positions(self):
        source = Path("app/cfb_capper_preview.py").read_text(encoding="utf-8")

        self.assertIn("data-capper-last24h-toggle", source)
        self.assertIn("capperLast24hOnly&&kind==='signals'", source)
        self.assertIn("capperWithin24h(item.closed_at||item.submitted_at)", source)
        self.assertIn("Settled positions", source)
        self.assertIn("No settled positions in the last 24 hours.", source)
        self.assertIn("cfbPositionList", source)


if __name__ == "__main__":
    unittest.main()


def test_cfb_capper_dashboard_has_persistent_online_controls():
    source = Path("app/cfb_capper_preview.py").read_text(encoding="utf-8")

    assert '"/api/cfb-cappers/sport-enabled"' in source
    assert '"/api/cfb-cappers/enabled/{capper_key}"' in source
    assert "capper_control.is_enabled(core, SPORT_CONTROL_LABEL)" in source
    assert "capper_control.is_enabled(core, source_label)" in source
    assert '"PAUSED_SPORT"' in source
    assert '"PAUSED_CAPPER"' in source
    assert "cfbToggleSport" in source
    assert "cfbToggleCapper" in source
    assert '<div class="capper-panel-title">CFB AUTO-TRADING</div>' in source
    assert 'id="cfbSportPower"' in source
    assert 'id="cfbCapperPower-slam"' in source
    assert 'id="cfbCapperPower-syndicate"' in source
