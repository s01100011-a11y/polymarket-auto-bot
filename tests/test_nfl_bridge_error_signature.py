from app import nfl_capper_ingest as capper


def test_feed_signature_changes_when_bridge_freshness_changes():
    left = {
        "scanned_posts": 0,
        "detected_posts": 0,
        "detected_picks": 0,
        "listener_connected": False,
        "listener_ready": False,
        "freshness": {
            "connected": False,
            "ready": False,
            "resolved_channels": [],
            "error": "first",
        },
        "picks": [],
        "unparsed_recent": [],
    }
    right = dict(left)
    right["freshness"] = dict(left["freshness"], error="second")
    assert capper._feed_content_signature(left) != capper._feed_content_signature(right)
