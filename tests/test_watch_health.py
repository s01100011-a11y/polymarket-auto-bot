import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.responses import JSONResponse

from app import main


class WatchHealthTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(main.WATCH_HEALTH)
        main.WATCH_HEALTH.clear()
        main.WATCH_HEALTH.update({
            "loop_started_at": None,
            "last_started_at": None,
            "last_completed_at": None,
            "last_error": None,
            "cycles": 0,
        })

    def tearDown(self):
        main.WATCH_HEALTH.clear()
        main.WATCH_HEALTH.update(self.original)

    def test_startup_without_timestamp_is_temporarily_healthy(self):
        now = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
        snap = main._watch_health_snapshot(now)
        self.assertTrue(snap["healthy"])
        self.assertFalse(snap["stale"])

    def test_recent_completed_cycle_is_healthy(self):
        now = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
        main.WATCH_HEALTH["last_completed_at"] = (now - timedelta(seconds=5)).isoformat()
        snap = main._watch_health_snapshot(now)
        self.assertTrue(snap["healthy"])

    def test_stale_completed_cycle_is_unhealthy(self):
        now = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
        age = max(30, main.AUTO_POLL_SECONDS * 3) + 1
        main.WATCH_HEALTH["last_completed_at"] = (now - timedelta(seconds=age)).isoformat()
        snap = main._watch_health_snapshot(now)
        self.assertFalse(snap["healthy"])
        self.assertTrue(snap["stale"])

    def test_hung_first_cycle_becomes_unhealthy(self):
        now = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
        age = max(30, main.AUTO_POLL_SECONDS * 3) + 1
        main.WATCH_HEALTH["loop_started_at"] = (now - timedelta(seconds=age + 10)).isoformat()
        main.WATCH_HEALTH["last_started_at"] = (now - timedelta(seconds=age)).isoformat()
        snap = main._watch_health_snapshot(now)
        self.assertFalse(snap["healthy"])

    def test_health_endpoint_returns_503_when_watch_loop_is_stale(self):
        stale = {
            "healthy": False,
            "stale": True,
            "stale_after_seconds": 90,
            "age_seconds": 91.0,
            **main.WATCH_HEALTH,
        }
        with patch.object(main, "_watch_health_snapshot", return_value=stale):
            response = main.health()
        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
