import unittest

from app import slack_ingest as ingest
from app import slack_wnba


class SlackBasketballFailoverTests(unittest.TestCase):
    def test_basketball_mentions_include_nba_and_wnba(self):
        self.assertEqual(
            ingest._basketball_team_mentions("Boston Celtics vs New York Knicks"),
            ["Boston Celtics", "New York Knicks"],
        )
        self.assertEqual(
            ingest._basketball_team_mentions("Minnesota Lynx vs Seattle Storm"),
            ["Minnesota Lynx", "Seattle Storm"],
        )

    def test_league_detection_routes_nba_and_wnba(self):
        self.assertEqual(ingest._league_for_team("Boston Celtics"), "nba")
        self.assertEqual(ingest._league_for_team("Minnesota Lynx"), "wnba")
        self.assertIsNone(ingest._league_for_team("Unknown Team"))

    def test_predicted_winner_parser_accepts_nba(self):
        parsed = slack_wnba._parse_alert(
            "Predicted Winner — Boston Celtics\n"
            "82% win probability\n"
            "Game: Boston Celtics vs New York Knicks · Q4\n"
            "Score: 101-96 · BK Odds: -240"
        )
        self.assertTrue(parsed["actionable"])
        self.assertEqual(parsed["selection"], "Boston Celtics")
        self.assertEqual(parsed["teams"], ["Boston Celtics", "New York Knicks"])
        self.assertEqual(parsed["quarter"], "Q4")


    def test_direct_feed_predicted_winner_format_accepts_nba_and_builds_dedup_key(self):
        parsed = slack_wnba._parse_alert(
            "NBA PW Alert\n"
            "Q4\n"
            "Predicted Winner: *Boston Celtics* (82% win probability)\n"
            "BK ML: -240\n"
            "https://site.api.espn.com/gameId/401999999\n"
            "2026-09-27 02:30:00 +08"
        )
        self.assertTrue(parsed["actionable"])
        self.assertEqual(parsed["selection"], "Boston Celtics")
        self.assertEqual(parsed["pw"]["game_id"], "401999999")
        self.assertEqual(parsed["pw"]["quarter"], "Q4")
        self.assertEqual(parsed["pw"]["win_probability"], 82)
        self.assertIsNotNone(ingest._pw_signal_key(parsed))

    def test_predicted_winner_parser_keeps_wnba(self):
        parsed = slack_wnba._parse_alert(
            "Predicted Winner — Minnesota Lynx\n"
            "78% win probability\n"
            "Game: Minnesota Lynx vs Seattle Storm · Q3"
        )
        self.assertTrue(parsed["actionable"])
        self.assertEqual(parsed["selection"], "Minnesota Lynx")
        self.assertEqual(parsed["teams"], ["Minnesota Lynx", "Seattle Storm"])


if __name__ == "__main__":
    unittest.main()
