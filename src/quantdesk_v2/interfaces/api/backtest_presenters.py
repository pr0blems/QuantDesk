"""Stable JSON presenters for persisted backtest runs."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ...models import BacktestRun, BacktestTrade


def _utc_iso(value: datetime) -> str:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _float_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return float(result) if result.is_finite() else None


def backtest_run_summary(run: BacktestRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "run_id": run.id,
        "public_id": run.public_id,
        "user_strategy_id": run.user_strategy_id,
        "strategy_revision_id": run.strategy_revision_id,
        "strategy_id": run.strategy_id,
        "strategy_name": run.strategy_name,
        "symbol": run.symbol,
        "timeframe": run.timeframe,
        "status": run.status,
        "start_at": _utc_iso(run.start_at),
        "end_at": _utc_iso(run.end_at),
        "start_date": run.start_at.date().isoformat(),
        "end_date": run.end_at.date().isoformat(),
        "initial_capital": _float_value(run.initial_capital),
        "final_equity": _float_value(run.final_equity),
        "net_profit": _float_value(run.net_profit),
        "total_return_pct": _float_value(run.total_return_pct),
        "max_drawdown_pct": _float_value(run.max_drawdown_pct),
        "sharpe_ratio": _float_value(run.sharpe_ratio),
        "win_rate_pct": _float_value(run.win_rate_pct),
        "profit_factor": _float_value(run.profit_factor),
        "trade_count": run.trade_count,
        "metrics_json": _json_safe(run.metrics_json or {}),
        "error": run.error,
        "created_at": _utc_iso(run.created_at),
        "completed_at": _utc_iso(run.completed_at) if run.completed_at else None,
    }


def backtest_trade_response(trade: BacktestTrade) -> dict[str, Any]:
    return {
        **_json_safe(trade.metadata_json or {}),
        "id": trade.id,
        "side": trade.side,
        "entry_ts": int(trade.entry_at.replace(tzinfo=UTC).timestamp()),
        "exit_ts": int(trade.exit_at.replace(tzinfo=UTC).timestamp()),
        "entry_at": _utc_iso(trade.entry_at),
        "exit_at": _utc_iso(trade.exit_at),
        "entry_price": _float_value(trade.entry_price),
        "exit_price": _float_value(trade.exit_price),
        "quantity": _float_value(trade.quantity),
        "gross_pnl": _float_value(trade.gross_pnl),
        "fees": _float_value(trade.fees),
        "net_pnl": _float_value(trade.net_pnl),
        "return_pct": _float_value(trade.return_pct),
        "holding_bars": trade.holding_bars,
        "exit_reason": trade.exit_reason,
    }


def backtest_run_detail(run: BacktestRun) -> dict[str, Any]:
    metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
    stored_account = metadata.get("account") if isinstance(metadata.get("account"), dict) else {}
    account = {
        **_json_safe(stored_account),
        "initial_capital": _float_value(run.initial_capital),
        "final_equity": _float_value(run.final_equity),
        "net_profit": _float_value(run.net_profit),
    }
    metrics = _json_safe(run.metrics_json or {})
    metrics.update(
        {
            "net_profit": _float_value(run.net_profit),
            "total_return_pct": _float_value(run.total_return_pct),
            "max_drawdown_pct": _float_value(run.max_drawdown_pct),
            "sharpe_ratio": _float_value(run.sharpe_ratio),
            "win_rate_pct": _float_value(run.win_rate_pct),
            "profit_factor": _float_value(run.profit_factor),
            "trade_count": run.trade_count,
        }
    )
    data_quality = _json_safe(run.data_quality_json or {})
    returned_count = metadata.get("response_trade_count")
    if not isinstance(returned_count, int):
        returned_count = (run.data_quality_json or {}).get("trades_returned")
    if isinstance(returned_count, int) and returned_count >= 0:
        response_trades = run.trades[-returned_count:] if returned_count else []
    else:
        response_trades = run.trades
    return {
        "run": backtest_run_summary(run),
        "result": {
            "account": account,
            "metrics": metrics,
            "equity_curve": _json_safe(run.equity_curve_json or []),
            "price_candles": (
                data_quality.get("price_candles", [])
                if isinstance(data_quality, dict)
                else []
            ),
            "trades": [backtest_trade_response(trade) for trade in response_trades],
            "data_quality": data_quality,
        },
    }
