import unittest

from app.pw_game_reconstruction import derive_game_assets, nearest_price


class GameReconstructionTests(unittest.TestCase):
    def test_derive_assets_from_away_mapping(self):
        assets = derive_game_assets(
            "SEA",
            "NY",
            [{
                "status": "OK",
                "pick_abbr": "SEA",
                "asset_id": "away-token",
                "opposite_asset_id": "home-token",
                "outcome_label": "Seattle Storm",
                "opposite_outcome_label": "New York Liberty",
                "event_slug": "wnba-sea-ny-test",
                "market_id": "m1",
                "condition_id": "c1",
            }],
        )
        self.assertTrue(assets["mapped"])
        self.assertEqual(assets["away_asset_id"], "away-token")
        self.assertEqual(assets["home_asset_id"], "home-token")

    def test_derive_assets_from_home_mapping(self):
        assets = derive_game_assets(
            "SEA",
            "NY",
            [{
                "status": "OK",
                "pick_abbr": "NY",
                "asset_id": "home-token",
                "opposite_asset_id": "away-token",
                "outcome_label": "New York Liberty",
                "opposite_outcome_label": "Seattle Storm",
            }],
        )
        self.assertTrue(assets["mapped"])
        self.assertEqual(assets["away_asset_id"], "away-token")
        self.assertEqual(assets["home_asset_id"], "home-token")

    def test_nearest_price_prefers_known_price_before_play(self):
        points = [(100, 0.40), (160, 0.45), (220, 0.51)]
        mark = nearest_price(points, 190, max_lag_seconds=90)
        self.assertEqual(mark["ts"], 160)
        self.assertEqual(mark["alignment"], "at_or_before")
        self.assertEqual(mark["lag_s"], 30)

    def test_nearest_price_falls_forward_when_no_recent_prior(self):
        points = [(100, 0.40), (220, 0.51)]
        mark = nearest_price(points, 190, max_lag_seconds=45)
        self.assertEqual(mark["ts"], 220)
        self.assertEqual(mark["alignment"], "after")
        self.assertEqual(mark["lag_s"], 30)

    def test_nearest_price_rejects_stale_marks(self):
        points = [(100, 0.40), (400, 0.60)]
        self.assertIsNone(nearest_price(points, 250, max_lag_seconds=90))


if __name__ == "__main__":
    unittest.main()
