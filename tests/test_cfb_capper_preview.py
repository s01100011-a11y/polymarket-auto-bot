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
        self.assertFalse(calls[0][1]["auto"])


if __name__ == "__main__":
    unittest.main()
