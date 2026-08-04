from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from quantdesk_v2.models import PaperAccount, User
from quantdesk_v2.monitor import MonitorRepository
from quantdesk_v2.strategy_catalog import (
    ensure_user_default_strategies,
    strategy_snapshot,
)


def build_monitor_fixture(engine: Engine, tmp_path) -> tuple[MonitorRepository, int, int]:
    symbols = tmp_path / "symbols.json"
    symbols.write_text(
        json.dumps({"symbols": [{"symbol": "TESTUSDT", "underlyingType": "stock"}]}),
        encoding="utf-8",
    )

    with Session(engine) as db:
        user = User(
            username="monitor-user",
            password_hash="test-only-hash",  # noqa: S106 - isolated test fixture
        )
        db.add(user)
        db.flush()
        strategy = ensure_user_default_strategies(db, user.id)[0]
        account = PaperAccount(
            user_id=user.id,
            strategy_id=strategy.id,
            name="monitor-paper-account",
            initial_balance=Decimal("10000"),
            balance=Decimal("10000"),
            config_json={},
            strategy_snapshot_json=strategy_snapshot(strategy),
        )
        db.add(account)
        db.commit()
        user_id = user.id
        account_id = account.id

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ticker(symbol,price,pct_24h,quote_volume,ts)
                VALUES(:symbol,:price,:pct_24h,:quote_volume,:ts)
                """
            ),
            {
                "symbol": "TESTUSDT",
                "price": 101.5,
                "pct_24h": 2.25,
                "quote_volume": 5_000,
                "ts": 2_000_000_000,
            },
        )
        connection.execute(
            text(
                """INSERT INTO contract_price_move_buckets(
                       symbol,bucket_ts,up_count,down_count
                   ) VALUES('TESTUSDT',:bucket_ts,7,4)"""
            ),
            {"bucket_ts": int(time.time()) - 60},
        )
        connection.execute(
            text(
                """
                INSERT INTO scores(symbol,tf,open_time,score,detail)
                VALUES(:symbol,:tf,:open_time,:score,:detail)
                """
            ),
            [
                {
                    "symbol": "TESTUSDT",
                    "tf": timeframe,
                    "open_time": 1,
                    "score": score,
                    "detail": "[]",
                }
                for timeframe, score in (("15m", 60), ("1h", 80), ("4h", 40))
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO alerts(
                    id,user_id,ts,symbol,kind,direction,score,message,detail,`read`
                ) VALUES(
                    1,:user_id,1,'TESTUSDT','score','long',80,'test alert',NULL,0
                )
                """
            ),
            {"user_id": user_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO klines(symbol,tf,open_time,open,high,low,close,volume)
                VALUES('TESTUSDT','1h',1,100,103,99,101.5,20)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO market_opportunities(
                    public_id,scanner_key,scanner_version,symbol,primary_timeframe,
                    direction,status,quality_score,detected_bar_time,expires_bar_time,
                    evidence_json,dedup_key,created_at,updated_at
                ) VALUES(
                    '11111111-1111-1111-1111-111111111111','test-scanner',1,
                    'TESTUSDT','15m','long','confirmed',88.5,1000,2800,
                    :evidence,'test-opportunity',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "evidence": json.dumps(
                    {
                        "summary": "测试多头机会",
                        "reason_codes": ["REGIME_TRENDING", "STRUCTURE_BREAKOUT"],
                        "conditions": {"trigger_aligned": True},
                    }
                )
            },
        )
    return MonitorRepository(engine, symbols), user_id, account_id


def test_monitor_overview_breadth_and_user_state(mysql_test_engine, tmp_path) -> None:
    repository, user_id, _ = build_monitor_fixture(mysql_test_engine, tmp_path)
    overview = repository.overview(["TESTUSDT"])
    assert len(overview["items"]) == 1
    assert overview["items"][0]["watch"] is True
    assert overview["items"][0]["score"] == 62
    assert overview["items"][0]["opportunity"]["direction"] == "long"
    assert overview["items"][0]["opportunity"]["quality_score"] == 88.5
    assert overview["items"][0]["green_flashes_30m"] == 7
    assert overview["items"][0]["red_flashes_30m"] == 4
    assert repository.breadth()["bull"] == 1
    assert repository.alerts(user_id, 10)[0]["read"] is False
    repository.mark_alerts_read(user_id)
    assert repository.alerts(user_id, 10)[0]["read"] is True
    assert repository.latest_alert_id(user_id) == 1


def test_monitor_detail_queries(mysql_test_engine, tmp_path) -> None:
    repository, _, _ = build_monitor_fixture(mysql_test_engine, tmp_path)
    assert repository.klines("testusdt", "1h", 120)[0]["close"] == 101.5
    assert repository.score_detail("TESTUSDT")["1h"]["score"] == 80


def test_opportunity_preferences_are_isolated_by_user(mysql_test_engine, tmp_path) -> None:
    repository, user_id, _ = build_monitor_fixture(mysql_test_engine, tmp_path)
    with Session(mysql_test_engine) as db:
        other = User(
            username="other-monitor-user",
            password_hash="test-only-hash",  # noqa: S106 - isolated test fixture
        )
        db.add(other)
        db.commit()
        other_user_id = other.id
    with mysql_test_engine.begin() as connection:
        opportunity_id = connection.execute(
            text(
                "SELECT id FROM market_opportunities "
                "WHERE public_id='11111111-1111-1111-1111-111111111111'"
            )
        ).scalar_one()
        connection.execute(
            text(
                """INSERT INTO user_opportunity_states(
                       user_id,opportunity_id,state,notify_enabled,last_viewed_at,
                       created_at,updated_at
                   ) VALUES(:user_id,:opportunity_id,'ignored',1,CURRENT_TIMESTAMP,
                            CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"""
            ),
            {"user_id": user_id, "opportunity_id": opportunity_id},
        )

    assert repository.opportunities(user_id, 10) == []
    ignored = repository.opportunities(user_id, 10, include_ignored=True)
    assert ignored[0]["user_state"] == "ignored"
    visible_to_other = repository.opportunities(other_user_id, 10)
    assert visible_to_other[0]["user_state"] is None


def test_paper_account_uses_shared_mysql_engine(mysql_test_engine, tmp_path) -> None:
    from quantdesk import store as market_store

    repository, user_id, account_id = build_monitor_fixture(mysql_test_engine, tmp_path)
    account = repository.paper(user_id, account_id)
    assert market_store.get_engine() is mysql_test_engine
    assert account["account"]["equity"] == 10_000
    assert account["positions"] == []

    market_store.execute(
        """
        INSERT INTO paper_trades(
            paper_account_id,user_id,symbol,side,pnl,fee,closed_ts
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (account_id, user_id, "TESTUSDT", 1, 10, 1, 1),
    )
    reset = repository.reset_paper(user_id, account_id)
    assert reset["trades"] == []
    assert reset["account"]["balance"] == 10_000


def test_paper_performance_aggregates_calendar_in_requested_timezone(
    mysql_test_engine, tmp_path
) -> None:
    from quantdesk import store as market_store

    repository, user_id, account_id = build_monitor_fixture(mysql_test_engine, tmp_path)
    repository.paper(user_id, account_id)
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
        market_store.execute(
            """
            INSERT INTO paper_trades(
                paper_account_id,user_id,symbol,side,pnl,fee,closed_ts
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (account_id, user_id, "TESTUSDT", 1, pnl, fee, closed_ts),
        )
    for ts, equity in ((1, 10_000), (2, 11_000), (3, 8_800)):
        market_store.execute(
            """
            REPLACE INTO paper_equity(
                paper_account_id,user_id,ts,equity,balance
            ) VALUES(?,?,?,?,?)
            """,
            (account_id, user_id, ts, equity, equity),
        )

    result = repository.paper_performance(user_id, account_id, "2026-08", 480)

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
