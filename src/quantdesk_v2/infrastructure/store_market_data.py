"""MarketDataFeed backed by QuantDesk's existing market store."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .. import market_store
from ..domain.trading import Bar, Quote


class StoreMarketDataFeed:
    """Read current quotes and closed bars without leaking SQL into strategies."""

    def latest_quote(self, symbol: str) -> Quote | None:
        normalized = str(symbol).strip().upper()
        rows = market_store.query(
            "SELECT symbol,price,ts FROM ticker WHERE symbol=? AND price IS NOT NULL LIMIT 1",
            (normalized,),
        )
        if not rows:
            return None
        row = rows[0]
        try:
            observed_at = datetime.fromtimestamp(int(row["ts"]), tz=UTC)
            return Quote(
                symbol=str(row["symbol"]),
                price=_decimal(row["price"]),
                observed_at=observed_at,
            )
        except (KeyError, OSError, OverflowError, TypeError, ValueError) as exc:
            raise ValueError("invalid ticker row") from exc

    def bars(self, symbol: str, timeframe: str, *, limit: int) -> tuple[Bar, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 5_000:
            raise ValueError("bar limit must be between 1 and 5000")
        normalized_symbol = str(symbol).strip().upper()
        normalized_timeframe = str(timeframe).strip().lower()
        rows = market_store.get_klines(normalized_symbol, normalized_timeframe, limit)
        try:
            return tuple(
                Bar(
                    symbol=normalized_symbol,
                    timeframe=normalized_timeframe,
                    open_time_ms=int(row["open_time"]),
                    open=_decimal(row["open"]),
                    high=_decimal(row["high"]),
                    low=_decimal(row["low"]),
                    close=_decimal(row["close"]),
                    volume=_decimal(row["volume"]),
                )
                for row in rows
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid kline row") from exc


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("invalid market-data decimal")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid market-data decimal") from None
    if not parsed.is_finite():
        raise ValueError("invalid market-data decimal")
    return parsed
