import json
import sqlite3
from datetime import UTC, datetime

from quantdesk_v2.monitor import MonitorRepository


def build_monitor_fixture(tmp_path):
    database = tmp_path / "monitor.db"
    symbols = tmp_path / "symbols.json"
    symbols.write_text(
        json.dumps({"symbols": [{"symbol": "TESTUSDT", "underlyingType": "stock"}]}),
        encoding="utf-8",
    )
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE ticker(symbol TEXT PRIMARY KEY, price REAL, pct_24h REAL, quote_volume REAL, ts INTEGER);
        CREATE TABLE scores(symbol TEXT, tf TEXT, open_time INTEGER, score REAL, detail TEXT);
        CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE alerts(id INTEGER PRIMARY KEY, ts INTEGER, symbol TEXT, kind TEXT, direction TEXT, score REAL, message TEXT, detail TEXT, read INTEGER);
        CREATE TABLE news(id TEXT PRIMARY KEY, ts INTEGER, source TEXT, lang TEXT, title TEXT, title_zh TEXT, link TEXT, sentiment TEXT, summary TEXT);
        CREATE TABLE klines(symbol TEXT, tf TEXT, open_time INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL);
        """
    )
    connection.execute(
        "INSERT INTO ticker VALUES(?, ?, ?, ?, ?)",
        ("TESTUSDT", 101.5, 2.25, 5000, 2_000_000_000),
    )
    for timeframe, score in (("15m", 60), ("1h", 80), ("4h", 40)):
        connection.execute(
            "INSERT INTO scores VALUES(?, ?, ?, ?, ?)",
            ("TESTUSDT", timeframe, 1, score, "[]"),
        )
    connection.execute(
        "INSERT INTO alerts VALUES(1, 1, 'TESTUSDT', 'score', 'long', 80, 'test alert', NULL, 0)"
    )
    connection.execute("INSERT INTO klines VALUES('TESTUSDT', '1h', 1, 100, 103, 99, 101.5, 20)")
    connection.commit()
    connection.close()
    return MonitorRepository(database, symbols)


def test_monitor_overview_breadth_and_user_state(tmp_path) -> None:
    repository = build_monitor_fixture(tmp_path)
    overview = repository.overview(["TESTUSDT"])
    assert len(overview["items"]) == 1
    assert overview["items"][0]["watch"] is True
    assert overview["items"][0]["score"] == 62
    assert repository.breadth()["bull"] == 1
    assert repository.alerts(10, 0)[0]["read"] is False
    assert repository.alerts(10, 1)[0]["read"] is True
    assert repository.latest_alert_id() == 1


def test_monitor_detail_queries(tmp_path) -> None:
    repository = build_monitor_fixture(tmp_path)
    assert repository.klines("testusdt", "1h", 120)[0]["close"] == 101.5
    assert repository.score_detail("TESTUSDT")["1h"]["score"] == 80


def test_paper_account_uses_legacy_store_database(tmp_path) -> None:
    from quantdesk import store as legacy_store

    repository = build_monitor_fixture(tmp_path)
    original_path = legacy_store.DB_PATH
    try:
        account = repository.paper()
        assert account["account"]["equity"] == 10_000
        assert account["positions"] == []

        legacy_store.execute(
            "INSERT INTO paper_trades(symbol, side, pnl, fee, closed_ts) VALUES(?, ?, ?, ?, ?)",
            ("TESTUSDT", 1, 10, 1, 1),
        )
        reset = repository.reset_paper()
        assert reset["trades"] == []
        assert reset["account"]["balance"] == 10_000
    finally:
        if legacy_store._conn is not None:
            legacy_store._conn.close()
            legacy_store._conn = None
        legacy_store.DB_PATH = original_path


def test_paper_performance_aggregates_calendar_in_requested_timezone(tmp_path) -> None:
    from quantdesk import store as legacy_store

    repository = build_monitor_fixture(tmp_path)
    original_path = legacy_store.DB_PATH
    try:
        repository.paper()
        first_close = int(datetime(2026, 8, 2, 16, 30, tzinfo=UTC).timestamp())
        second_close = int(datetime(2026, 8, 3, 1, 0, tzinfo=UTC).timestamp())
        third_close = int(datetime(2026, 8, 3, 17, 0, tzinfo=UTC).timestamp())
        fourth_close = int(datetime(2026, 8, 3, 18, 0, tzinfo=UTC).timestamp())
        for pnl, fee, closed_ts in (
            (10, 1, first_close),
            (-4, 1, second_close),
            (20, 2, third_close),
            (1, 1, fourth_close),
        ):
            legacy_store.execute(
                "INSERT INTO paper_trades(symbol, side, pnl, fee, closed_ts) VALUES(?, ?, ?, ?, ?)",
                ("TESTUSDT", 1, pnl, fee, closed_ts),
            )
        for ts, equity in ((1, 10_000), (2, 11_000), (3, 8_800)):
            legacy_store.execute(
                "INSERT OR REPLACE INTO paper_equity(ts, equity, balance) VALUES(?, ?, ?)",
                (ts, equity, equity),
            )

        result = repository.paper_performance("2026-08", 480)

        assert result["metrics"]["realized_pnl"] == 22
        assert result["metrics"]["win_rate"] == 66.7
        assert result["metrics"]["profit_factor"] == 5.4
        assert result["metrics"]["average_profit"] == 5.5
        assert result["metrics"]["average_win"] == 13.5
        assert result["metrics"]["breakeven"] == 1
        assert result["metrics"]["max_drawdown"] == 20
        assert result["metrics"]["max_drawdown_basis"] == "since_reset_full_equity"
        assert result["calendar"]["total_pnl"] == 22
        assert result["calendar"]["days"] == [
            {
                "date": "2026-08-03",
                "pnl": 4.0,
                "trades": 2,
                "wins": 1,
                "losses": 1,
                "breakeven": 0,
            },
            {
                "date": "2026-08-04",
                "pnl": 18.0,
                "trades": 2,
                "wins": 1,
                "losses": 0,
                "breakeven": 1,
            },
        ]
    finally:
        if legacy_store._conn is not None:
            legacy_store._conn.close()
            legacy_store._conn = None
        legacy_store.DB_PATH = original_path
