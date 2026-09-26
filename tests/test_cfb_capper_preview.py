from __future__ import annotations

import unittest
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

    def test_two_units_is_twenty_dollars(self):
        self.assertEqual(capper._stake_for_pick(_pick(units=2)), Decimal("20.00"))
        self.assertEqual(capper._stake_for_pick(_pick(units=None)), Decimal("10.00"))

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
            with self.assertRaisesRegex(ValueError, "no unique nearby game"):
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

        events = [event("cfb-baylor-a"), event("cfb-baylor-b")]

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
        self.assertIn('BETTER LINE', rendered)

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
            "match_status": "MATCHED",
            "market_type": "moneyline",
            "event_slug": "cfb-clemson-unc-2026-09-26",
            "event_title": "Clemson vs UNC",
            "event_start_at": "2020-01-01T00:00:00+00:00",
            "event_start_checked_at": "2026-09-26T01:00:00+00:00",
            "event_closed": False,
            "market_accepting_orders": True,
            "market": "Clemson vs UNC",
            "market_url": "https://polymarket.com/sports/cfb/cfb-clemson-unc-2026-09-26",
            "outcome": "Clemson",
            "asset_id": "clemson-token",
        }
        with (
            patch.object(capper, "_resolve_market_match", return_value=refreshed),
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


class CfbSpreadAlternateTests(unittest.TestCase):
    @staticmethod
    def _market(question, label, token, *, accepting=True):
        yes = SimpleNamespace(label=label, token_id=token)
        no = SimpleNamespace(label="Indiana", token_id=token + "-no")
        return SimpleNamespace(
            id=token + "-market",
            question=question,
            slug=token + "-spread",
            sports=SimpleNamespace(sports_market_type="spread", line=None),
            outcomes=SimpleNamespace(yes=yes, no=no),
            state=SimpleNamespace(accepting_orders=accepting),
        )

    def test_spread_outcome_any_line_reads_explicit_selected_team_line(self):
        market = self._market(
            "Northwestern +21.5 vs Indiana",
            "Northwestern",
            "nw-215",
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
        self.assertEqual(str(selected[2]), "21.5")

    def test_find_spread_alternatives_prefers_better_equidistant_line(self):
        event = SimpleNamespace(
            id="nw-ind",
            slug="cfb-nw-ind-2026-09-25",
            title="Northwestern vs Indiana",
            start_time="2020-01-01T00:00:00+00:00",
            markets=[
                self._market("Northwestern +20.5 vs Indiana", "Northwestern", "nw-205"),
                self._market("Northwestern +21.5 vs Indiana", "Northwestern", "nw-215"),
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
            "nw-205": {
                "current_buy_price": "0.50", "best_ask": "0.51", "max_price": "0.51",
                "spread": "0.02", "live_odds_american": "+96", "quote_updated_at": "now",
            },
            "nw-215": {
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
        self.assertEqual(result["stake_usdc"], "20.00")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "PREVIEW")
        self.assertEqual(calls[0][1]["market_type"], "total")
        self.assertEqual(calls[0][1]["budget_usdc"], "20.00")
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


if __name__ == "__main__":
    unittest.main()
