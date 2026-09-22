from datetime import datetime, timedelta, timezone

from app import main


def make_signal(signal_id="sig-1", expires_at=None):
    return main.AutoSignal(
        market_url="https://polymarket.com/event/example",
        outcome="Team A",
        market_type="moneyline",
        max_price="0.50",
        budget_usdc="5",
        signal_id=signal_id,
        category="sports",
        expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )


def isolate_files(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "WATCH_FILE", tmp_path / "watchlist.json")
    monkeypatch.setattr(main, "EXECUTIONS_FILE", tmp_path / "executions.json")


def test_watch_loop_expires_signal(tmp_path, monkeypatch):
    isolate_files(tmp_path, monkeypatch)
    sig = make_signal(expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    main._save(main.WATCH_FILE, {
        sig.signal_id: {"signal_id": sig.signal_id, "status": "WATCHING", "signal": sig.model_dump(mode="json")}
    })
    main._process_watchlist_once()
    assert main._load(main.WATCH_FILE)[sig.signal_id]["status"] == "EXPIRED"


def test_watch_loop_marks_successful_execution(tmp_path, monkeypatch):
    isolate_files(tmp_path, monkeypatch)
    sig = make_signal()
    main._save(main.WATCH_FILE, {
        sig.signal_id: {"signal_id": sig.signal_id, "status": "WATCHING", "signal": sig.model_dump(mode="json")}
    })
    monkeypatch.setattr(main, "_try_execute_signal", lambda s: {"status": "ORDER_SUBMITTED", "id": s.signal_id})
    main._process_watchlist_once()
    rec = main._load(main.WATCH_FILE)[sig.signal_id]
    assert rec["status"] == "ORDER_SUBMITTED"
    assert rec["last_result"]["id"] == sig.signal_id


def test_watch_loop_recovers_record_to_error_on_exception(tmp_path, monkeypatch):
    isolate_files(tmp_path, monkeypatch)
    sig = make_signal()
    main._save(main.WATCH_FILE, {
        sig.signal_id: {"signal_id": sig.signal_id, "status": "WATCHING", "signal": sig.model_dump(mode="json")}
    })

    def boom(_):
        raise RuntimeError("test failure")
    monkeypatch.setattr(main, "_try_execute_signal", boom)
    main._process_watchlist_once()
    rec = main._load(main.WATCH_FILE)[sig.signal_id]
    assert rec["status"] == "ERROR"
    assert "test failure" in rec["last_error"]


def test_try_execute_signal_deduplicates_existing_execution(tmp_path, monkeypatch):
    isolate_files(tmp_path, monkeypatch)
    sig = make_signal()
    existing = {"id": sig.signal_id, "status": "ORDER_SUBMITTED"}
    main._save(main.EXECUTIONS_FILE, {sig.signal_id: existing})

    def quote_should_not_run(*args, **kwargs):
        raise AssertionError("quote should not be called for duplicate")
    monkeypatch.setattr(main, "_quote", quote_should_not_run)

    assert main._try_execute_signal(sig) == existing
