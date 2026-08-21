from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from quantdesk_v2.ai_monitor import (
    _finnhub_signal_quote_payload,
    _point_in_time_finnhub_quote,
)

ROOT = Path(__file__).resolve().parents[1]


def _quote(symbol: str, fetched_at: datetime, price: str = "336") -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        price=Decimal(price),
        change=Decimal("1.2"),
        change_percent=Decimal("0.36"),
        day_high=Decimal("338"),
        day_low=Decimal("330"),
        day_open=Decimal("331"),
        previous_close=Decimal("334.8"),
        volume=Decimal("123456"),
        source_timestamp=int(fetched_at.replace(tzinfo=UTC).timestamp()),
        fetched_at=fetched_at,
        live=True,
    )


def test_finnhub_signal_quote_is_explicitly_last_trade_only() -> None:
    signal_at = datetime(2026, 8, 17, 14, 21, 5)
    payload = _finnhub_signal_quote_payload(
        _quote("LRCX", datetime(2026, 8, 17, 14, 14, 20)),
        signal_at=signal_at,
    )

    assert payload["provider"] == "finnhub"
    assert payload["source"] == "finnhub_quote_snapshots"
    assert payload["market_session"] == "regular"
    assert payload["last_trade_only"] is True
    assert payload["nbbo_available"] is False
    assert payload["last_trade_age_ms"] == 405_000
    assert payload["last_price"] == 336.0
    assert "bid" not in payload
    assert "ask" not in payload


def test_point_in_time_finnhub_quote_never_uses_future_or_stale_rows() -> None:
    signal_at = datetime(2026, 8, 17, 14, 21, 5)
    old = _quote("LRCX", datetime(2026, 8, 17, 14, 0))
    valid = _quote("LRCX", datetime(2026, 8, 17, 14, 14, 20))
    future = _quote("LRCX", datetime(2026, 8, 17, 14, 22))

    selected = _point_in_time_finnhub_quote(
        {"LRCX": [old, valid, future]},
        symbol="lrcx",
        signal_at=signal_at,
    )

    assert selected is valid
    assert (
        _point_in_time_finnhub_quote(
            {"LRCX": [old]},
            symbol="LRCX",
            signal_at=signal_at,
        )
        is None
    )


def test_prediction_analytics_explains_partial_and_missing_domains() -> None:
    component = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(
        encoding="utf-8"
    )

    assert '<option value="partial">仅现货价快照</option>' in component
    assert '? "现货价快照（非盘口）"' in component
    assert 'reference_quote_stale: "参考盘口已过期"' in component
    assert 'uw_disabled_at_signal: "采集关闭"' in component
    assert 'legacy_snapshot_missing: "历史未冻结"' in component
    assert 'market_feature_not_linked: "信号时无快照"' in component
    assert '"last_trade_age_ms"' in component
