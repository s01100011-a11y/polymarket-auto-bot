import unittest
from types import SimpleNamespace

from app import pw_spread_capture as capture


def _market(question, line, *, accepting=True):
    yes = SimpleNamespace(label="Atlanta Dream", token_id="atl-token")
    no = SimpleNamespace(label="Chicago Sky", token_id="chi-token")
    return SimpleNamespace(
        id="spread-market",
        question=question,
        sports=SimpleNamespace(sports_market_type="spread", line=line),
        outcomes=SimpleNamespace(yes=yes, no=no),
        state=SimpleNamespace(accepting_orders=accepting),
        condition_id="condition-1",
    )


class PwSpreadCaptureTests(unittest.TestCase):
    def test_selected_question_team_keeps_signed_line(self):
        event = SimpleNamespace(
            markets=[_market("Spread: Atlanta Dream (-4.5)", "-4.5")]
        )
        rows = capture._spread_candidates(
            event,
            "Atlanta Dream",
            ("dream", "atl"),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["asset_id"], "atl-token")
        self.assertEqual(rows[0]["poly_line"], -4.5)

    def test_other_team_gets_complementary_line(self):
        event = SimpleNamespace(
            markets=[_market("Spread: Atlanta Dream (-4.5)", "-4.5")]
        )
        rows = capture._spread_candidates(
            event,
            "Chicago Sky",
            ("chicago sky", "sky", "chi"),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["asset_id"], "chi-token")
        self.assertEqual(rows[0]["poly_line"], 4.5)

    def test_closed_spread_is_not_captured(self):
        event = SimpleNamespace(
            markets=[_market("Spread: Atlanta Dream (-4.5)", "-4.5", accepting=False)]
        )
        rows = capture._spread_candidates(event, "Atlanta Dream", ("dream",))
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
