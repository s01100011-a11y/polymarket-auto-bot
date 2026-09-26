from pathlib import Path


def test_cfb_capper_requires_fresh_telegram_bridge():
    source = Path("app/cfb_capper_preview.py").read_text(encoding="utf-8")
    assert '"require_fresh": "true"' in source


def test_nfl_capper_requires_fresh_telegram_bridge():
    source = Path("app/nfl_capper_ingest.py").read_text(encoding="utf-8")
    assert '"require_fresh": "true"' in source
