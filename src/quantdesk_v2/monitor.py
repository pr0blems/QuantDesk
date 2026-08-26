from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


class MonitorUnavailable(RuntimeError):
    pass


def _bind_params(sql: str, params: tuple[Any, ...]):
    parts = sql.split("?")
    if len(parts) - 1 != len(params):
        raise MonitorUnavailable("invalid monitor query parameters")
    statement = "".join(
        part + (f":p{index}" if index < len(params) else "") for index, part in enumerate(parts)
    )
    return text(statement), {f"p{index}": value for index, value in enumerate(params)}


_REPORT_LOCK = threading.Lock()
_MARKET_MICROSTRUCTURE_STALE_SECONDS = 30
_MAX_MARKET_CLOCK_SKEW_SECONDS = 5
_UNDERLYING_SYMBOL_ALIASES = {
    "BRKB": "BRK.B",
    "PAYP": "PYPL",
}
_UNLISTED_UNDERLYINGS = {"ANTHROPIC", "CBRS", "OPENAI", "QNTX", "SPC", "SPCX"}
_SPREAD_WATCH_BPS = 25
_SPREAD_STRONG_BPS = 50


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _json_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _optional_finite_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _datetime_to_ms(value: Any) -> int:
    if isinstance(value, datetime):
        current = value if value.tzinfo else value.replace(tzinfo=UTC)
        return int(current.timestamp() * 1_000)
    if isinstance(value, str):
        try:
            current = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0
        current = current if current.tzinfo else current.replace(tzinfo=UTC)
        return int(current.timestamp() * 1_000)
    return 0


def _underlying_market_state(status: dict[str, Any]) -> str:
    if not status.get("available"):
        return "unknown"
    session = str(status.get("session") or "")
    if session == "pre-market":
        return "pre_market"
    if session == "regular":
        return "regular"
    if session == "post-market":
        return "after_hours"
    return "closed" if status.get("is_open") is False else "unknown"


def build_underlying_quote(
    symbol: str,
    symbol_metadata: dict[str, Any],
    contract_price: float | None,
    contract_timestamp: Any,
    quote_by_symbol: dict[str, dict[str, Any]],
    market_status: dict[str, Any],
) -> dict[str, Any]:
    """Map a TradFi contract to its spot quote and derive aligned basis state."""

    base = symbol.removesuffix("USDT").removesuffix("USD1")
    quote_symbol = _UNDERLYING_SYMBOL_ALIASES.get(base, base)
    relation = "alias" if quote_symbol != base else "direct"
    is_equity = str(symbol_metadata.get("underlyingType") or "").upper() == "EQUITY"
    unsupported = base in _UNLISTED_UNDERLYINGS or not is_equity
    quote = quote_by_symbol.get(quote_symbol, {}) if not unsupported else {}
    available = bool(quote.get("available"))
    quote_stale = bool(quote.get("stale"))
    status = (
        "unsupported"
        if unsupported
        else "stale"
        if available and quote_stale
        else "ok"
        if available
        else "unavailable"
    )
    contract_time_ms = int(contract_timestamp or 0)
    if 0 < contract_time_ms < 100_000_000_000:
        contract_time_ms *= 1_000
    market_time_ms = int(quote.get("source_timestamp") or 0)
    if 0 < market_time_ms < 100_000_000_000:
        market_time_ms *= 1_000
    alignment_delta_ms = (
        abs(contract_time_ms - market_time_ms) if contract_time_ms and market_time_ms else None
    )
    market_state = _underlying_market_state(market_status) if available else "unavailable"
    if not available or not contract_time_ms:
        alignment_status = "unavailable"
    elif market_state == "closed":
        alignment_status = "closed"
    elif status == "stale" or market_status.get("stale"):
        alignment_status = "stale"
    elif contract_time_ms // 60_000 == market_time_ms // 60_000:
        alignment_status = "aligned"
    else:
        alignment_status = "lagging"

    underlying_price = _optional_finite_number(quote.get("price")) if available else None
    basis_comparable = (
        contract_price is not None
        and underlying_price is not None
        and underlying_price > 0
        and relation in {"direct", "alias"}
        and status in {"ok", "stale"}
    )
    basis_bps = (
        round((contract_price / underlying_price - 1) * 10_000, 2) if basis_comparable else None
    )
    alert_eligible = status == "ok" and alignment_status == "aligned"
    spread_alert = (
        "strong"
        if alert_eligible and basis_bps is not None and abs(basis_bps) >= _SPREAD_STRONG_BPS
        else "watch"
        if alert_eligible and basis_bps is not None and abs(basis_bps) >= _SPREAD_WATCH_BPS
        else "normal"
        if alert_eligible and basis_bps is not None
        else "disabled"
    )
    return {
        "quote_symbol": None if unsupported else quote_symbol,
        "relation": "unlisted" if base in _UNLISTED_UNDERLYINGS else relation,
        "instrument_type": "equity" if is_equity else "unsupported",
        "display_name": f"{quote_symbol} 现货" if not unsupported else None,
        "source": "finnhub",
        "status": status,
        "market_state": market_state,
        "currency": "USD" if available else None,
        "exchange_name": "US",
        "price": underlying_price,
        "previous_close": _optional_finite_number(quote.get("previous_close")),
        "change_pct": _optional_finite_number(quote.get("change_percent")),
        "regular_market_price": underlying_price,
        "day_open": _optional_finite_number(quote.get("day_open")),
        "day_high": _optional_finite_number(quote.get("day_high")),
        "day_low": _optional_finite_number(quote.get("day_low")),
        "volume": _optional_finite_number(quote.get("volume")),
        "market_time_ms": market_time_ms,
        "received_at_ms": _datetime_to_ms(quote.get("fetched_at")),
        "basis_bps": basis_bps,
        "basis_comparable": basis_comparable,
        "spread_alert": spread_alert,
        "contract_time_ms": contract_time_ms,
        "alignment_delta_ms": alignment_delta_ms,
        "alignment_status": alignment_status,
        "stale": status != "ok",
    }


def build_collected_underlying_quote(
    row: dict[str, Any],
    contract_price: float | None,
    contract_timestamp: Any,
) -> dict[str, Any]:
    """Normalize a persisted Yahoo quote and calculate its aligned basis."""

    status = str(row.get("status") or "unavailable")
    has_market_data = bool(row.get("quote_symbol")) and status in {"ok", "stale"}
    contract_time_ms = int(contract_timestamp or 0)
    if 0 < contract_time_ms < 100_000_000_000:
        contract_time_ms *= 1_000
    market_time_ms = int(row.get("market_time_ms") or 0)
    alignment_delta_ms = (
        abs(contract_time_ms - market_time_ms) if contract_time_ms and market_time_ms else None
    )
    market_state = str(row.get("market_state") or "unknown")
    if not has_market_data or not contract_time_ms:
        alignment_status = "unavailable"
    elif market_state == "closed":
        alignment_status = "closed"
    elif status == "stale":
        alignment_status = "stale"
    elif contract_time_ms // 60_000 == market_time_ms // 60_000:
        alignment_status = "aligned"
    else:
        alignment_status = "lagging"

    underlying_price = _optional_finite_number(row.get("price")) if has_market_data else None
    currency = str(row.get("currency") or "") if has_market_data else ""
    basis_comparable = (
        contract_price is not None
        and underlying_price is not None
        and underlying_price > 0
        and currency == "USD"
        and str(row.get("relation") or "") in {"direct", "benchmark"}
        and status in {"ok", "stale"}
    )
    basis_bps = (
        round((contract_price / underlying_price - 1) * 10_000, 2) if basis_comparable else None
    )
    alert_eligible = status == "ok" and alignment_status == "aligned"
    spread_alert = (
        "strong"
        if alert_eligible and basis_bps is not None and abs(basis_bps) >= _SPREAD_STRONG_BPS
        else "watch"
        if alert_eligible and basis_bps is not None and abs(basis_bps) >= _SPREAD_WATCH_BPS
        else "normal"
        if alert_eligible and basis_bps is not None
        else "disabled"
    )
    return {
        "quote_symbol": row.get("quote_symbol"),
        "relation": row.get("relation"),
        "instrument_type": row.get("instrument_type"),
        "display_name": row.get("display_name") if has_market_data else None,
        "source": row.get("source"),
        "status": status,
        "market_state": market_state if has_market_data else "unavailable",
        "currency": currency or None,
        "exchange_name": row.get("exchange_name") if has_market_data else None,
        "price": underlying_price,
        "previous_close": _optional_finite_number(row.get("previous_close")),
        "change_pct": _optional_finite_number(row.get("change_pct")),
        "regular_market_price": _optional_finite_number(row.get("regular_market_price")),
        "day_open": _optional_finite_number(row.get("day_open")),
        "day_high": _optional_finite_number(row.get("day_high")),
        "day_low": _optional_finite_number(row.get("day_low")),
        "volume": _optional_finite_number(row.get("volume")),
        "market_time_ms": market_time_ms if has_market_data else 0,
        "received_at_ms": int(row.get("received_at_ms") or 0),
        "basis_bps": basis_bps,
        "basis_comparable": basis_comparable,
        "spread_alert": spread_alert,
        "contract_time_ms": contract_time_ms,
        "alignment_delta_ms": alignment_delta_ms,
        "alignment_status": alignment_status,
        "stale": status != "ok",
    }


def _fresh_microstructure_metrics(
    row: dict[str, Any] | None,
    *,
    now_seconds: int,
) -> dict[str, float | int]:
    """Expose a depth snapshot only while all of its metrics remain trustworthy."""

    if not row:
        return {}
    try:
        snapshot_at = int(row.get("ts") or 0)
        depth_levels = int(row.get("depth_levels"))
        bid_level_count = int(row.get("bid_level_count", depth_levels))
        ask_level_count = int(row.get("ask_level_count", depth_levels))
    except (TypeError, ValueError):
        return {}
    age_seconds = now_seconds - snapshot_at
    if not (-_MAX_MARKET_CLOCK_SKEW_SECONDS <= age_seconds <= _MARKET_MICROSTRUCTURE_STALE_SECONDS):
        return {}
    numbers = {
        key: _optional_finite_number(row.get(key))
        for key in (
            "bid_depth_notional",
            "ask_depth_notional",
            "book_imbalance",
            "book_imbalance_5",
        )
    }
    optional_numbers = {
        key: _optional_finite_number(row.get(key))
        for key in (
            "bid_depth_notional_5",
            "ask_depth_notional_5",
            "spread_bps",
            "bid_depth_change_5s_pct",
            "ask_depth_change_5s_pct",
            "bid_depth_change_30s_pct",
            "ask_depth_change_30s_pct",
            "imbalance_change_5s",
        )
    }
    if (
        any(value is None for value in numbers.values())
        or numbers["bid_depth_notional"] < 0
        or numbers["ask_depth_notional"] < 0
        or not -1 <= numbers["book_imbalance"] <= 1
        or not -1 <= numbers["book_imbalance_5"] <= 1
        or not 0 <= depth_levels <= 100
        or not 0 <= bid_level_count <= 100
        or not 0 <= ask_level_count <= 100
        or (
            optional_numbers["spread_bps"] is not None
            and optional_numbers["spread_bps"] < 0
        )
    ):
        return {}
    return {
        **numbers,
        **optional_numbers,
        "depth_levels": depth_levels,
        "bid_level_count": bid_level_count,
        "ask_level_count": ask_level_count,
    }


def _opportunity_out(row: dict[str, Any], *, compact: bool = False) -> dict[str, Any]:
    evidence = _json_object(row.get("evidence_json"))
    result = {
        "id": row.get("public_id"),
        "symbol": row.get("symbol"),
        "direction": row.get("direction"),
        "status": row.get("status"),
        "quality_score": _finite_number(row.get("quality_score")),
        "primary_timeframe": row.get("primary_timeframe"),
        "detected_bar_time": row.get("detected_bar_time"),
        "expires_bar_time": row.get("expires_bar_time"),
        "summary": evidence.get("summary"),
        "reason_codes": evidence.get("reason_codes") or [],
        "conditions": evidence.get("conditions") or {},
    }
    if not compact:
        result.update(
            {
                "scanner_key": row.get("scanner_key"),
                "scanner_version": row.get("scanner_version"),
                "evidence": evidence,
                "user_state": row.get("user_state"),
                "notify_enabled": bool(row.get("notify_enabled")),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
        )
    return result


class MonitorRepository:
    def __init__(self, engine: Engine, symbols_config: Path):
        if engine.dialect.name not in {"mysql", "mariadb"}:
            raise MonitorUnavailable("contract monitor data requires MySQL")
        self.engine = engine
        self.symbols_config = symbols_config.expanduser().resolve()
        if not self.symbols_config.is_file():
            raise MonitorUnavailable("contract monitor symbols config is unavailable")
        try:
            config = json.loads(self.symbols_config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MonitorUnavailable("contract monitor symbols config is invalid") from exc
        self.symbols_meta = config.get("symbols", [])
        self.symbols = [item["symbol"] for item in self.symbols_meta if item.get("symbol")]
        self.symbol_set = set(self.symbols)

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            statement, values = _bind_params(sql, params)
            with self.engine.connect() as connection:
                return [dict(row) for row in connection.execute(statement, values).mappings()]
        except SQLAlchemyError as exc:
            raise MonitorUnavailable("contract monitor data query failed") from exc

    def _validate_symbol(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        if normalized not in self.symbol_set:
            raise MonitorUnavailable("unknown contract monitor symbol")
        return normalized

    def _latest_score_rows(
        self,
        symbols: Sequence[str],
        timeframes: Sequence[str] = ("15m", "1h", "4h"),
    ) -> list[dict[str, Any]]:
        """Read the latest score for each requested symbol/timeframe by PK lookup.

        The former GROUP BY + self-join scanned the complete scores history on
        every monitor refresh.  That query can exceed the production MySQL read
        timeout while the market workers are writing.  The scores primary key
        is (symbol, tf, open_time), so scalar ORDER BY/LIMIT lookups are bounded
        and deterministic even as history grows.
        """
        statements: list[str] = []
        params: list[Any] = []
        for symbol in symbols:
            for timeframe in timeframes:
                statements.append(
                    """SELECT ? AS symbol, ? AS tf,
                              (SELECT score FROM scores
                               WHERE symbol=? AND tf=?
                               ORDER BY open_time DESC LIMIT 1) AS score"""
                )
                params.extend((symbol, timeframe, symbol, timeframe))
        if not statements:
            return []
        return [
            row
            for row in self._query(" UNION ALL ".join(statements), tuple(params))
            if row.get("score") is not None
        ]

    def _configure_market_store(self) -> None:
        """Point the in-process market modules at the shared MySQL engine."""
        from . import market_store

        market_store.configure_engine(self.engine)

    def overview(
        self,
        watchlist: list[str],
        *,
        symbols: Sequence[str] | None = None,
        underlying_quotes: dict[str, Any] | None = None,
        underlying_market_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from . import market_engine

        selected_symbols = (
            [self._validate_symbol(symbol) for symbol in symbols]
            if symbols is not None
            else list(self.symbols)
        )
        now_seconds = int(time.time())
        tickers = {row["symbol"]: row for row in self._query("SELECT * FROM ticker")}
        rolling_changes = market_engine.rolling_price_changes(
            selected_symbols,
            now=now_seconds,
            require_fresh_stream=True,
        )
        try:
            microstructure = {
                row["symbol"]: row
                for row in self._query(
                    """SELECT symbol,bid_depth_notional,ask_depth_notional,
                              bid_depth_notional_5,ask_depth_notional_5,
                              book_imbalance,book_imbalance_5,depth_levels,
                              bid_level_count,ask_level_count,spread_bps,
                              bid_depth_change_5s_pct,ask_depth_change_5s_pct,
                              bid_depth_change_30s_pct,ask_depth_change_30s_pct,
                              imbalance_change_5s,ts
                       FROM market_microstructure"""
                )
            }
        except MonitorUnavailable:
            # Keep a rolling deployment usable until the new table is migrated.
            microstructure = {}
        try:
            underlying_by_contract = {
                row["contract_symbol"]: row
                for row in self._query("SELECT * FROM underlying_market_quotes")
            }
        except MonitorUnavailable:
            # Fall back to the in-memory Finnhub source during rolling migration.
            underlying_by_contract = {}
        score_rows = self._latest_score_rows(selected_symbols)
        scores: dict[str, dict[str, float]] = {}
        for row in score_rows:
            scores.setdefault(row["symbol"], {})[row["tf"]] = row["score"]

        try:
            battle_rows = self._query(
                """SELECT p.*,f.quality_score FROM battle_predictions p
                   JOIN prediction_feature_snapshots f ON f.id=p.feature_snapshot_id
                   WHERE p.current_marker=1 ORDER BY p.symbol,p.horizon_seconds"""
            )
        except Exception:
            # Keep the monitor usable while a deployment is rolling through
            # the battle-prediction migration.
            battle_rows = []
        horizon_names = {300: "5m", 900: "15m", 3_600: "1h"}
        battles: dict[str, dict[str, dict[str, Any]]] = {}
        now_ms = int(time.time() * 1_000)
        for row in battle_rows:
            horizon = horizon_names.get(int(row["horizon_seconds"]), str(row["horizon_seconds"]))
            state = str(row["prediction_state"])
            if int(row.get("valid_until_ms") or 0) < now_ms:
                state = "data_insufficient"
            battles.setdefault(str(row["symbol"]), {})[horizon] = {
                "id": row.get("public_id"),
                "horizon_seconds": int(row["horizon_seconds"]),
                "state": state,
                "result": row.get("result"),
                "battle_score": _finite_number(row.get("battle_score")),
                "long": round(float(row.get("long_probability") or 0) * 100, 1),
                "short": round(float(row.get("short_probability") or 0) * 100, 1),
                "neutral": round(float(row.get("neutral_probability") or 0) * 100, 1),
                "confidence": {"low": "低", "medium": "中", "high": "高"}.get(
                    str(row.get("confidence_label")), "低"
                ),
                "confidence_score": _finite_number(row.get("confidence_score")),
                "data_quality": _finite_number(row.get("quality_score")),
                "predicted_at_ms": int(row.get("predicted_at_ms") or 0),
                "valid_until_ms": int(row.get("valid_until_ms") or 0),
            }

        opportunity_rows = self._query(
            """
            SELECT o.* FROM market_opportunities o
            JOIN (
                SELECT symbol, MAX(detected_bar_time) AS latest_bar
                FROM market_opportunities
                WHERE status IN ('detected','watching','confirmed')
                GROUP BY symbol
            ) latest
              ON latest.symbol=o.symbol AND latest.latest_bar=o.detected_bar_time
            WHERE o.status IN ('detected','watching','confirmed')
            """
        )
        opportunities = {
            row["symbol"]: _opportunity_out(row, compact=True) for row in opportunity_rows
        }

        trending_rows = self._query("SELECT v FROM system_state WHERE k='st_trending'")
        try:
            trending = (
                set(json.loads(trending_rows[0]["v"]).get("symbols", []))
                if trending_rows
                else set()
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            trending = set()

        metadata = {item["symbol"]: item for item in self.symbols_meta}
        quote_snapshot = underlying_quotes or {}
        quote_by_symbol = {
            str(item.get("symbol") or "").upper(): item
            for item in quote_snapshot.get("quotes", [])
            if isinstance(item, dict) and item.get("symbol")
        }
        market_status = underlying_market_status or {}
        selected = set(watchlist)
        weights = {"15m": 0.3, "1h": 0.4, "4h": 0.3}
        items = []
        latest = 0
        for symbol in selected_symbols:
            ticker = tickers.get(symbol, {})
            short_term = rolling_changes.get(symbol, {})
            depth = _fresh_microstructure_metrics(
                microstructure.get(symbol),
                now_seconds=now_seconds,
            )
            latest = max(latest, int(ticker.get("ts") or 0))
            tf_scores = scores.get(symbol, {})
            numerator = sum(
                tf_scores.get(tf, 0) * weight for tf, weight in weights.items() if tf in tf_scores
            )
            denominator = sum(weight for tf, weight in weights.items() if tf in tf_scores)
            base = symbol.replace("USDT", "").replace("USD1", "")
            symbol_metadata = metadata.get(symbol, {})
            price = _finite_number(ticker.get("price"))
            collected_underlying = underlying_by_contract.get(symbol)
            underlying_quote = (
                build_collected_underlying_quote(
                    collected_underlying,
                    price,
                    ticker.get("ts"),
                )
                if collected_underlying is not None
                else build_underlying_quote(
                    symbol,
                    symbol_metadata,
                    price,
                    ticker.get("ts"),
                    quote_by_symbol,
                    market_status,
                )
            )
            items.append(
                {
                    "symbol": symbol,
                    "underlying": symbol_metadata.get("underlyingType", ""),
                    "underlying_quote": underlying_quote,
                    "price": price,
                    "pct_2m": short_term.get("pct_2m"),
                    "pct_5m": short_term.get("pct_5m"),
                    "pct_10m": short_term.get("pct_10m"),
                    "pct_24h": _finite_number(ticker.get("pct_24h")),
                    "quote_volume": _finite_number(ticker.get("quote_volume")),
                    "bid_depth_notional": depth.get("bid_depth_notional"),
                    "ask_depth_notional": depth.get("ask_depth_notional"),
                    "bid_depth_notional_5": depth.get("bid_depth_notional_5"),
                    "ask_depth_notional_5": depth.get("ask_depth_notional_5"),
                    "book_imbalance": depth.get("book_imbalance"),
                    "book_imbalance_5": depth.get("book_imbalance_5"),
                    "depth_levels": depth.get("depth_levels"),
                    "bid_level_count": depth.get("bid_level_count"),
                    "ask_level_count": depth.get("ask_level_count"),
                    "spread_bps": depth.get("spread_bps"),
                    "bid_depth_change_5s_pct": depth.get("bid_depth_change_5s_pct"),
                    "ask_depth_change_5s_pct": depth.get("ask_depth_change_5s_pct"),
                    "bid_depth_change_30s_pct": depth.get("bid_depth_change_30s_pct"),
                    "ask_depth_change_30s_pct": depth.get("ask_depth_change_30s_pct"),
                    "imbalance_change_5s": depth.get("imbalance_change_5s"),
                    "score": round(numerator / denominator) if denominator else None,
                    "tf_scores": tf_scores,
                    "watch": symbol in selected,
                    "position": None,
                    "trending": base in trending,
                    "opportunity": opportunities.get(symbol),
                    "green_flashes_30m": 0,
                    "red_flashes_30m": 0,
                    "battle": battles.get(symbol, {}),
                }
            )
        return {
            "items": items,
            "updated_at": latest,
            "stale": not latest or time.time() - latest > 30,
        }

    def opportunities(
        self,
        user_id: int,
        limit: int,
        *,
        symbol: str | None = None,
        direction: str | None = None,
        include_expired: bool = False,
        include_ignored: bool = False,
    ) -> list[dict[str, Any]]:
        normalized_symbol = self._validate_symbol(symbol) if symbol else None
        normalized_direction = None
        if direction:
            normalized_direction = direction.strip().lower()
            if normalized_direction not in {"long", "short", "neutral"}:
                raise MonitorUnavailable("unknown opportunity direction")
        rows = self._query(
            """
            SELECT o.*,uos.state AS user_state,uos.notify_enabled
            FROM market_opportunities o
            LEFT JOIN user_opportunity_states uos
              ON uos.opportunity_id=o.id AND uos.user_id=?
            WHERE (?=1 OR o.status IN ('detected','watching','confirmed'))
              AND (? IS NULL OR o.symbol=?)
              AND (? IS NULL OR o.direction=?)
              AND (?=1 OR uos.state IS NULL OR uos.state<>'ignored')
            ORDER BY CASE o.status WHEN 'confirmed' THEN 0 WHEN 'watching' THEN 1 ELSE 2 END,
                     o.quality_score DESC,o.detected_bar_time DESC,o.id DESC
            LIMIT ?
            """,
            (
                user_id,
                int(include_expired),
                normalized_symbol,
                normalized_symbol,
                normalized_direction,
                normalized_direction,
                int(include_ignored),
                limit,
            ),
        )
        return [_opportunity_out(row) for row in rows]

    def breadth(self) -> dict[str, Any]:
        rows = self._latest_score_rows(self.symbols, ("1h",))
        bull = sum(1 for row in rows if row["score"] >= 40)
        bear = sum(1 for row in rows if row["score"] <= -40)
        neutral = len(rows) - bull - bear
        if not rows:
            return {
                "bull": 0,
                "bear": 0,
                "neutral": 0,
                "total": 0,
                "conclusion": "数据收集中…",
                "color": "#77808f",
            }
        if bull > bear * 2 and bull >= 5:
            conclusion = f"市场整体偏多（{bull}多 / {bear}空 / {neutral}中性）"
            color = "#2ebd85"
        elif bear > bull * 2 and bear >= 5:
            conclusion = f"市场整体偏空（{bull}多 / {bear}空 / {neutral}中性）"
            color = "#f6465d"
        else:
            conclusion = f"市场多空分歧（{bull}多 / {bear}空 / {neutral}中性）"
            color = "#f0b90b"
        return {
            "bull": bull,
            "bear": bear,
            "neutral": neutral,
            "total": len(rows),
            "conclusion": conclusion,
            "color": color,
        }

    def intelligence(self) -> dict[str, Any]:
        """Return monitor feedback from the intelligence data available in V2."""
        now_seconds = int(time.time())
        try:
            fresh_depth_rows = self._query(
                """SELECT symbol FROM market_microstructure
                   WHERE ts>=? AND ts<=?""",
                (
                    now_seconds - _MARKET_MICROSTRUCTURE_STALE_SECONDS,
                    now_seconds + _MAX_MARKET_CLOCK_SKEW_SECONDS,
                ),
            )
        except MonitorUnavailable:
            fresh_depth_rows = []
        lifecycle_rows = self._query(
            """SELECT COUNT(*) active,
                      SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) confirmed,
                      COUNT(DISTINCT scanner_key) scanners
               FROM market_opportunities
               WHERE status IN ('detected','watching','confirmed')"""
        )
        outcome_rows = self._query(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN o.status='completed' THEN 1 ELSE 0 END) completed,
                      SUM(CASE WHEN o.status='pending' THEN 1 ELSE 0 END) pending,
                      AVG(CASE WHEN o.status='completed' AND p.result IN ('long','short')
                               THEN o.directional_return_bps END) avg_return_bps,
                      AVG(CASE WHEN o.status='completed' AND p.result IN ('long','short')
                                    AND o.directional_return_bps>0 THEN 1
                               WHEN o.status='completed' AND p.result IN ('long','short') THEN 0
                               ELSE NULL END) hit_rate
               FROM prediction_outcomes o
               JOIN battle_predictions p ON p.id=o.prediction_id"""
        )
        scanner_rows = self._query(
            """SELECT p.model_key AS scanner_key,p.horizon_seconds,COUNT(*) samples,
                      AVG(o.directional_return_bps) avg_return_bps,
                      AVG(CASE WHEN o.directional_return_bps>0 THEN 1 ELSE 0 END) hit_rate,
                      AVG(o.max_favorable_bps) avg_mfe_bps,
                      AVG(o.max_adverse_bps) avg_mae_bps
               FROM prediction_outcomes o
               JOIN battle_predictions p ON p.id=o.prediction_id
               WHERE o.status='completed'
               GROUP BY p.model_key,p.horizon_seconds
               ORDER BY p.horizon_seconds,p.model_key"""
        )
        lifecycle = lifecycle_rows[0] if lifecycle_rows else {}
        outcomes = outcome_rows[0] if outcome_rows else {}
        total_symbols = len(self.symbols)
        fresh = len(self.symbol_set & {str(row.get("symbol") or "") for row in fresh_depth_rows})
        return {
            "market_data": {
                "symbols": total_symbols,
                "fresh_microstructure": fresh,
                "coverage_pct": round(fresh / total_symbols * 100, 2) if total_symbols else 0,
                "avg_spread_bps": None,
                "quality_events_24h": 0,
            },
            "opportunities": {
                "active": int(lifecycle.get("active") or 0),
                "confirmed": int(lifecycle.get("confirmed") or 0),
                "scanners": int(lifecycle.get("scanners") or 0),
            },
            "outcomes": {
                "total": int(outcomes.get("total") or 0),
                "completed": int(outcomes.get("completed") or 0),
                "pending": int(outcomes.get("pending") or 0),
                "avg_return_bps": _finite_number(outcomes.get("avg_return_bps")),
                "hit_rate": _finite_number(outcomes.get("hit_rate")),
            },
            "scanner_metrics": [
                {
                    **row,
                    "avg_return_bps": _finite_number(row.get("avg_return_bps")),
                    "hit_rate": _finite_number(row.get("hit_rate")),
                    "avg_mfe_bps": _finite_number(row.get("avg_mfe_bps")),
                    "avg_mae_bps": _finite_number(row.get("avg_mae_bps")),
                }
                for row in scanner_rows
            ],
            "shadow_execution": {
                "intents": 0,
                "filled": 0,
                "rejected": 0,
                "live_locked": True,
            },
            "targets": {
                "market_coverage_pct": 99.0,
                "candidate_label_coverage_pct": 100.0,
                "notice": "指标为工程验收目标，不代表收益承诺。",
            },
        }

    def prediction_history(
        self,
        page: int,
        page_size: int = 50,
        *,
        direction: str | None = None,
        horizon_seconds: int | None = None,
        hit: str | None = None,
        predicted_after_ms: int | None = None,
        predicted_before_ms: int | None = None,
        timezone_offset_minutes: int = 0,
    ) -> dict[str, Any]:
        """Return one newest-first page of battle predictions and their labels."""
        if direction not in {None, "long", "short"}:
            raise MonitorUnavailable("unknown prediction direction")
        if horizon_seconds not in {None, 300, 900, 3_600}:
            raise MonitorUnavailable("unknown prediction horizon")
        if hit not in {None, "hit", "miss"}:
            raise MonitorUnavailable("unknown prediction hit filter")
        if predicted_after_ms is not None and predicted_after_ms < 0:
            raise MonitorUnavailable("invalid prediction history start time")
        if predicted_before_ms is not None and predicted_before_ms < 0:
            raise MonitorUnavailable("invalid prediction history end time")
        if (
            predicted_after_ms is not None
            and predicted_before_ms is not None
            and predicted_after_ms >= predicted_before_ms
        ):
            raise MonitorUnavailable("invalid prediction history time range")
        if not -840 <= timezone_offset_minutes <= 840:
            raise MonitorUnavailable("invalid prediction history timezone offset")
        filter_params = (
            direction,
            direction,
            horizon_seconds,
            horizon_seconds,
            predicted_after_ms,
            predicted_after_ms,
            predicted_before_ms,
            predicted_before_ms,
            hit,
            hit,
            hit,
        )
        total_rows = self._query(
            """SELECT COUNT(*) total,
                       SUM(CASE WHEN p.result='long' THEN 1 ELSE 0 END) long_count,
                       SUM(CASE WHEN p.result='short' THEN 1 ELSE 0 END) short_count,
                       AVG(CASE WHEN o.directional_return_bps>0 THEN 1 ELSE 0 END) hit_rate,
                       AVG(o.directional_return_bps) avg_return_bps
               FROM battle_predictions p
               JOIN prediction_outcomes o ON o.prediction_id=p.id
               WHERE o.status='completed' AND p.result IN ('long','short')
                 AND (? IS NULL OR p.result=?)
                 AND (? IS NULL OR p.horizon_seconds=?)
                 AND (? IS NULL OR p.predicted_at_ms>=?)
                 AND (? IS NULL OR p.predicted_at_ms<?)
                 AND (? IS NULL OR (?='hit' AND o.directional_return_bps>0)
                                OR (?='miss' AND o.directional_return_bps<=0))""",
            filter_params,
        )
        totals = total_rows[0] if total_rows else {}
        total = int(totals.get("total") or 0)
        pages = max(1, math.ceil(total / page_size))
        current_page = min(max(1, page), pages)
        offset = (current_page - 1) * page_size
        timezone_offset_ms = timezone_offset_minutes * 60 * 1_000
        hourly_rows = self._query(
            """SELECT FLOOR((p.predicted_at_ms+?)/3600000) hour_bucket,
                      COUNT(*) total,
                      AVG(CASE WHEN o.directional_return_bps>0 THEN 1 ELSE 0 END) hit_rate,
                      AVG(o.directional_return_bps) avg_return_bps
               FROM battle_predictions p
               JOIN prediction_outcomes o ON o.prediction_id=p.id
               WHERE o.status='completed' AND p.result IN ('long','short')
                 AND (? IS NULL OR p.result=?)
                 AND (? IS NULL OR p.horizon_seconds=?)
                 AND (? IS NULL OR p.predicted_at_ms>=?)
                 AND (? IS NULL OR p.predicted_at_ms<?)
                 AND (? IS NULL OR (?='hit' AND o.directional_return_bps>0)
                                OR (?='miss' AND o.directional_return_bps<=0))
               GROUP BY hour_bucket ORDER BY hour_bucket""",
            (timezone_offset_ms, *filter_params),
        )
        hourly_by_bucket = {int(row["hour_bucket"]): row for row in hourly_rows}
        if predicted_after_ms is not None and predicted_before_ms is not None:
            first_bucket = math.floor((predicted_after_ms + timezone_offset_ms) / 3_600_000)
            final_bucket = math.ceil((predicted_before_ms + timezone_offset_ms) / 3_600_000)
            hourly_buckets = range(first_bucket, final_bucket)
        else:
            hourly_buckets = sorted(hourly_by_bucket)[-168:]
        hourly_statistics = []
        for bucket in hourly_buckets:
            hourly = hourly_by_bucket.get(bucket, {})
            hourly_statistics.append(
                {
                    "hour_start_ms": bucket * 3_600_000 - timezone_offset_ms,
                    "total": int(hourly.get("total") or 0),
                    "hit_rate": (
                        None
                        if hourly.get("hit_rate") is None
                        else _finite_number(hourly["hit_rate"])
                    ),
                    "avg_return_bps": (
                        None
                        if hourly.get("avg_return_bps") is None
                        else _finite_number(hourly["avg_return_bps"])
                    ),
                }
            )
        rows = self._query(
            """SELECT p.public_id,p.symbol,p.horizon_seconds,p.prediction_state,
                      p.result AS prediction_result,p.battle_score,p.long_probability,
                      p.short_probability,p.neutral_probability,p.confidence_score,
                      p.confidence_label,p.gross_edge_bps,p.entry_price,p.spread_bps,
                      p.target_bps,p.stop_bps,p.model_key,p.model_version,
                      a.algorithm_config_json,p.components_json,
                      f.feature_schema_version,
                      p.predicted_at_ms,p.valid_until_ms,
                      o.status,o.actual_result,o.exit_price,o.raw_return_bps,
                      o.directional_return_bps,o.max_favorable_bps,o.max_adverse_bps,
                      o.hit_result,o.cost_bps,o.due_at_ms,o.completed_at_ms
               FROM battle_predictions p
               JOIN prediction_feature_snapshots f ON f.id=p.feature_snapshot_id
               LEFT JOIN prediction_algorithm_snapshots a ON a.prediction_id=p.id
               JOIN prediction_outcomes o ON o.prediction_id=p.id
               WHERE o.status='completed' AND p.result IN ('long','short')
                 AND (? IS NULL OR p.result=?)
                 AND (? IS NULL OR p.horizon_seconds=?)
                 AND (? IS NULL OR p.predicted_at_ms>=?)
                 AND (? IS NULL OR p.predicted_at_ms<?)
                 AND (? IS NULL OR (?='hit' AND o.directional_return_bps>0)
                                OR (?='miss' AND o.directional_return_bps<=0))
               ORDER BY p.predicted_at_ms DESC,p.id DESC
               LIMIT ? OFFSET ?""",
            (*filter_params, page_size, offset),
        )
        numeric_fields = (
            "battle_score",
            "long_probability",
            "short_probability",
            "neutral_probability",
            "confidence_score",
            "gross_edge_bps",
            "entry_price",
            "spread_bps",
            "target_bps",
            "stop_bps",
            "exit_price",
            "raw_return_bps",
            "directional_return_bps",
            "max_favorable_bps",
            "max_adverse_bps",
            "cost_bps",
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            algorithm_config = _json_object(item.pop("algorithm_config_json", None))
            components = _json_object(item.pop("components_json", None))
            config_version = algorithm_config.get(
                "config_version", components.get("algorithm_config_version", 0)
            )
            try:
                item["algorithm_config_version"] = int(config_version or 0)
            except (TypeError, ValueError):
                item["algorithm_config_version"] = 0
            item["algorithm_snapshot_available"] = bool(algorithm_config)
            for field in numeric_fields:
                item[field] = None if item.get(field) is None else _finite_number(item[field])
            direction = str(item.get("prediction_result") or "neutral")
            directional_return = item.get("directional_return_bps")
            item["direction_hit"] = (
                directional_return > 0
                if item.get("status") == "completed"
                and direction in {"long", "short"}
                and directional_return is not None
                else None
            )
            items.append(item)
        return {
            "items": items,
            "page": current_page,
            "page_size": page_size,
            "pages": pages,
            "total": total,
            "hourly_statistics": hourly_statistics,
            "statistics": {
                "total": total,
                "long_count": int(totals.get("long_count") or 0),
                "short_count": int(totals.get("short_count") or 0),
                "hit_rate": (
                    None if totals.get("hit_rate") is None else _finite_number(totals["hit_rate"])
                ),
                "avg_return_bps": (
                    None
                    if totals.get("avg_return_bps") is None
                    else _finite_number(totals["avg_return_bps"])
                ),
            },
        }

    def prediction_algorithm_snapshot(self, public_id: str) -> dict[str, Any] | None:
        """Return the immutable inputs and algorithm config used by one prediction."""
        rows = self._query(
            """SELECT p.public_id,p.symbol,p.horizon_seconds,p.prediction_state,
                      p.result AS prediction_result,p.battle_score,p.model_key,p.model_version,
                      p.predicted_at_ms,p.reason_codes_json,p.components_json,
                      a.algorithm_config_json,f.feature_schema_version,f.features_json,
                      f.quality_score
               FROM battle_predictions p
               JOIN prediction_feature_snapshots f ON f.id=p.feature_snapshot_id
               LEFT JOIN prediction_algorithm_snapshots a ON a.prediction_id=p.id
               WHERE p.public_id=? LIMIT 1""",
            (public_id,),
        )
        if not rows:
            return None
        item = dict(rows[0])
        algorithm_config = _json_object(item.pop("algorithm_config_json", None))
        components = _json_object(item.pop("components_json", None))
        config_version = algorithm_config.get(
            "config_version", components.get("algorithm_config_version", 0)
        )
        try:
            item["algorithm_config_version"] = int(config_version or 0)
        except (TypeError, ValueError):
            item["algorithm_config_version"] = 0
        item["algorithm_snapshot_available"] = bool(algorithm_config)
        item["algorithm_config"] = algorithm_config or None
        item["components"] = components
        item["features"] = _json_object(item.pop("features_json", None))
        item["reason_codes"] = _json_array(item.pop("reason_codes_json", None))
        item["battle_score"] = _finite_number(item.get("battle_score"))
        item["quality_score"] = _finite_number(item.get("quality_score"))
        return item

    def prediction_algorithm_history(self) -> list[dict[str, Any]]:
        """Return settled immutable snapshots used by algorithm optimizers."""

        from . import battle

        return self._query(
            """SELECT recent.* FROM (
                   SELECT p.id AS prediction_row_id,p.symbol,p.horizon_seconds,
                          p.predicted_at_ms,p.result AS prediction_result,
                          p.battle_score,p.long_probability,p.short_probability,
                          p.neutral_probability,p.confidence_score,p.confidence_label,
                          p.gross_edge_bps,p.spread_bps,p.target_bps,p.stop_bps,
                          a.algorithm_config_json,f.features_json,
                          o.raw_return_bps,o.directional_return_bps,
                          o.max_favorable_bps,o.max_adverse_bps,o.hit_result,
                          o.cost_bps,o.completed_at_ms,
                          CAST(UNIX_TIMESTAMP(o.updated_at)*1000 AS SIGNED)
                              AS outcome_updated_at_ms
                   FROM battle_predictions p
                   JOIN prediction_feature_snapshots f ON f.id=p.feature_snapshot_id
                   JOIN prediction_algorithm_snapshots a ON a.prediction_id=p.id
                   JOIN prediction_outcomes o ON o.prediction_id=p.id
                   WHERE p.model_key=? AND p.model_version=?
                     AND f.feature_schema_version=?
                     AND p.prediction_state='heuristic'
                     AND o.status='completed' AND o.raw_return_bps IS NOT NULL
                   ORDER BY p.predicted_at_ms DESC,p.id DESC
                   LIMIT 50000
               ) recent
               ORDER BY recent.predicted_at_ms ASC,recent.prediction_row_id ASC""",
            (battle.MODEL_KEY, battle.MODEL_VERSION, battle.FEATURE_SCHEMA_VERSION),
        )

    def prediction_algorithm_optimization(
        self,
        current_config: dict[str, Any],
        current_config_version: int,
    ) -> dict[str, Any]:
        """Build the legacy local recommendation for diagnostic compatibility."""

        from .prediction_optimizer import optimize_prediction_algorithm

        return optimize_prediction_algorithm(
            self.prediction_algorithm_history(),
            current_config,
            current_config_version=current_config_version,
        )

    def alerts(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM alerts WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (user_id, limit),
        )
        for row in rows:
            row["read"] = bool(row.get("read"))
        return rows

    def latest_alert_id(self, user_id: int) -> int:
        rows = self._query(
            "SELECT COALESCE(MAX(id), 0) AS id FROM alerts WHERE user_id=?", (user_id,)
        )
        return int(rows[0]["id"]) if rows else 0

    def mark_alerts_read(self, user_id: int) -> None:
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text("UPDATE alerts SET `read`=1 WHERE user_id=:user_id"),
                    {"user_id": user_id},
                )
        except SQLAlchemyError as exc:
            raise MonitorUnavailable("contract monitor alert update failed") from exc

    def news(self, limit: int) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM news ORDER BY ts DESC LIMIT ?", (limit,))

    def latest_tickers(
        self, symbols: Sequence[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Return lightweight live quote snapshots for read-only position views."""

        requested = {
            str(symbol).strip().upper() for symbol in (symbols or self.symbols) if symbol
        }
        rows = self._query(
            "SELECT symbol,price,pct_24h,quote_volume,ts FROM ticker"
        )
        return {
            str(row["symbol"]).upper(): row
            for row in rows
            if str(row.get("symbol") or "").upper() in requested
        }

    def market_snapshot(self, symbol: str) -> dict[str, Any]:
        """Return one lightweight market snapshot for the browser WebSocket.

        The market worker writes Binance mini-ticker updates from its upstream
        WebSocket into ``ticker`` and publishes the synchronized depth book via
        shared storage.  This read path therefore stays bounded to one symbol,
        prefers the shared WebSocket book, and only lets ``ws_depth`` use its
        rate-limited REST cache when the live book is unavailable.
        """

        from . import ws_depth

        normalized = self._validate_symbol(symbol)
        ticker_rows = self._query(
            "SELECT symbol,price,pct_24h,quote_volume,ts FROM ticker WHERE symbol=?",
            (normalized,),
        )
        ticker = ticker_rows[0] if ticker_rows else {}
        try:
            collector_rows = self._query(
                "SELECT details_json,last_success_at,heartbeat_at "
                "FROM collector_status WHERE name='ticker' LIMIT 1"
            )
        except MonitorUnavailable:
            collector_rows = []
        collector = collector_rows[0] if collector_rows else {}
        try:
            collector_details = json.loads(collector.get("details_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            collector_details = {}
        ticker_transport = str(collector_details.get("source") or "server_cache")

        depth: dict[str, Any] = {}
        try:
            depth = ws_depth.order_book_snapshot(normalized, 100)
        except ws_depth.OrderBookUnavailableError:
            # Final local fallback: retain a still-fresh persisted metric row.
            try:
                rows = self._query(
                    """SELECT symbol,bid_depth_notional,ask_depth_notional,
                              bid_depth_notional_5,ask_depth_notional_5,
                              book_imbalance,book_imbalance_5,depth_levels,
                              bid_level_count,ask_level_count,spread_bps,
                              bid_depth_change_5s_pct,ask_depth_change_5s_pct,
                              bid_depth_change_30s_pct,ask_depth_change_30s_pct,
                              imbalance_change_5s,ts
                       FROM market_microstructure WHERE symbol=?""",
                    (normalized,),
                )
                depth = _fresh_microstructure_metrics(
                    rows[0] if rows else None,
                    now_seconds=int(time.time()),
                )
                if depth:
                    depth["transport"] = "database_cache"
                    depth["captured_at"] = int(rows[0].get("ts") or 0)
            except MonitorUnavailable:
                depth = {}

        now_seconds = int(time.time())
        ticker_at = int(ticker.get("ts") or 0)
        depth_at = int(depth.get("captured_at") or 0)
        depth_transport = str(depth.get("transport") or "unavailable")
        return {
            "symbol": normalized,
            "price": _finite_number(ticker.get("price")),
            "pct_24h": _finite_number(ticker.get("pct_24h")),
            "quote_volume": _finite_number(ticker.get("quote_volume")),
            "ticker_updated_at": ticker_at or None,
            "ticker_age_seconds": max(0, now_seconds - ticker_at) if ticker_at else None,
            "ticker_transport": ticker_transport,
            "bid_depth_notional": _finite_number(depth.get("bid_depth_notional")),
            "ask_depth_notional": _finite_number(depth.get("ask_depth_notional")),
            "book_imbalance": _finite_number(depth.get("book_imbalance")),
            "best_bid": _finite_number(depth.get("best_bid")),
            "best_ask": _finite_number(depth.get("best_ask")),
            "spread_bps": _finite_number(depth.get("spread_bps")),
            "depth_levels": min(
                100,
                int(depth.get("depth_levels") or depth.get("limit") or 0),
            ),
            "depth_updated_at": depth_at or None,
            "depth_age_seconds": max(0, now_seconds - depth_at) if depth_at else None,
            "depth_transport": depth_transport,
            "transport": (
                "websocket"
                if ticker_transport == "websocket"
                else "rest_fallback"
                if ticker_transport == "rest_fallback"
                else "server_cache"
            ),
        }

    def klines(self, symbol: str, timeframe: str, limit: int) -> list[dict[str, Any]]:
        normalized = self._validate_symbol(symbol)
        rows = self._query(
            """
            SELECT open_time, open, high, low, close, volume FROM klines
            WHERE symbol=? AND tf=? ORDER BY open_time DESC LIMIT ?
            """,
            (normalized, timeframe, limit),
        )
        return list(reversed(rows))

    def kline_range(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        """Return ascending candles inside a bounded historical window."""

        normalized = self._validate_symbol(symbol)
        if timeframe not in {"15m", "1h", "4h"}:
            raise MonitorUnavailable("unsupported monitor timeframe")
        return self._query(
            """
            SELECT open_time, open, high, low, close, volume FROM klines
            WHERE symbol=? AND tf=? AND open_time>=? AND open_time<=?
            ORDER BY open_time ASC
            """,
            (normalized, timeframe, int(start_ms), int(end_ms)),
        )

    def strategy_indicators(self, symbol: str, timeframe: str) -> dict[str, Any]:
        from . import indicators
        from .prediction_feature_indicators import evaluate_prediction_feature_indicators
        from .strategy_indicators import evaluate_directional_strategy_indicators

        normalized = self._validate_symbol(symbol)
        candles = self.klines(normalized, timeframe, 120)
        result = evaluate_directional_strategy_indicators(candles, timeframe)
        if candles:
            highs = [float(item["high"]) for item in candles]
            lows = [float(item["low"]) for item in candles]
            closes = [float(item["close"]) for item in candles]
            atr14 = indicators.atr(highs, lows, closes, 14)
            close = closes[-1]
            result["risk_metrics"] = {
                "atr14": round(float(atr14), 12) if atr14 is not None else None,
                "atr_pct": (
                    round(float(atr14) / close * 100, 8)
                    if atr14 is not None and close > 0
                    else None
                ),
                "close": close,
            }
        else:
            result["risk_metrics"] = {
                "atr14": None,
                "atr_pct": None,
                "close": None,
            }
        snapshot: dict[str, Any] | None = None
        try:
            rows = self._query(
                """SELECT as_of_ms,feature_schema_version,features_json,quality_score
                   FROM prediction_feature_snapshots
                   WHERE symbol=? ORDER BY as_of_ms DESC,id DESC LIMIT 1""",
                (normalized,),
            )
            if rows:
                snapshot = {
                    **rows[0],
                    "features": _json_object(rows[0].get("features_json")),
                }
        except MonitorUnavailable:
            # The K-line indicators remain usable during a rolling battle migration.
            snapshot = None
        prediction_features = evaluate_prediction_feature_indicators(snapshot, timeframe)
        result["prediction_features"] = prediction_features
        result["total_count"] = result["count"] + prediction_features["count"]
        return result

    def score_detail(self, symbol: str) -> dict[str, Any]:
        normalized = self._validate_symbol(symbol)
        rows = self._query(
            """
            SELECT s.tf, s.score, s.detail, s.open_time FROM scores s
            JOIN (SELECT tf, MAX(open_time) mo FROM scores WHERE symbol=? GROUP BY tf) m
            ON s.tf=m.tf AND s.open_time=m.mo WHERE s.symbol=?
            """,
            (normalized, normalized),
        )
        return {
            row["tf"]: {
                "score": row["score"],
                "open_time": row["open_time"],
                "factors": json.loads(row["detail"] or "[]"),
            }
            for row in rows
        }

    def report(self, symbol: str) -> dict[str, Any]:
        normalized = self._validate_symbol(symbol)
        with _REPORT_LOCK:
            from . import report as market_report

            self._configure_market_store()
            return market_report.build_report(normalized)

    def paper(
        self, user_id: int, account_id: int, timezone_offset_minutes: int = 0
    ) -> dict[str, Any]:
        with _REPORT_LOCK:
            from . import paper_engine

            self._configure_market_store()
            return paper_engine.api_data(user_id, account_id, timezone_offset_minutes)

    def paper_performance(
        self, user_id: int, account_id: int, month: str, timezone_offset_minutes: int
    ) -> dict[str, Any]:
        """Build a dashboard-safe performance summary for one tenant-owned paper account."""
        snapshot = self.paper(user_id, account_id)
        account = snapshot.get("account", {})
        start_balance = _finite_number(account.get("start"))
        try:
            year, month_number = (int(part) for part in month.split("-", maxsplit=1))
            local_start = datetime(year, month_number, 1, tzinfo=UTC)
        except (TypeError, ValueError) as exc:
            raise MonitorUnavailable("invalid performance calendar month") from exc
        if not 2000 <= year <= 2100:
            raise MonitorUnavailable("invalid performance calendar month")
        if month_number == 12:
            local_end = datetime(year + 1, 1, 1, tzinfo=UTC)
        else:
            local_end = datetime(year, month_number + 1, 1, tzinfo=UTC)
        offset = timedelta(minutes=timezone_offset_minutes)
        start_ts = int((local_start - offset).timestamp())
        end_ts = int((local_end - offset).timestamp())

        aggregate_rows = self._query(
            """
            SELECT COUNT(*) AS trades,
                   SUM(CASE WHEN COALESCE(pnl, 0) - COALESCE(fee, 0) > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN COALESCE(pnl, 0) - COALESCE(fee, 0) < 0 THEN 1 ELSE 0 END) AS losses,
                   SUM(CASE WHEN COALESCE(pnl, 0) - COALESCE(fee, 0) = 0 THEN 1 ELSE 0 END) AS breakeven,
                   SUM(CASE WHEN COALESCE(pnl, 0) - COALESCE(fee, 0) > 0
                            THEN COALESCE(pnl, 0) - COALESCE(fee, 0) ELSE 0 END) AS gross_profit,
                   ABS(SUM(CASE WHEN COALESCE(pnl, 0) - COALESCE(fee, 0) < 0
                                THEN COALESCE(pnl, 0) - COALESCE(fee, 0) ELSE 0 END)) AS gross_loss,
                   SUM(COALESCE(pnl, 0) - COALESCE(fee, 0)) AS realized_pnl
            FROM paper_trades
            WHERE paper_account_id=? AND user_id=?
            """,
            (account_id, user_id),
        )
        aggregate = aggregate_rows[0] if aggregate_rows else {}
        trades = int(aggregate.get("trades") or 0)
        wins = int(aggregate.get("wins") or 0)
        losses = int(aggregate.get("losses") or 0)
        breakeven = int(aggregate.get("breakeven") or 0)
        gross_profit = _finite_number(aggregate.get("gross_profit"))
        gross_loss = _finite_number(aggregate.get("gross_loss"))
        realized_pnl = _finite_number(aggregate.get("realized_pnl"))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
        if not trades:
            profit_factor_status = "no_trades"
        elif not gross_loss:
            profit_factor_status = "no_losses"
        else:
            profit_factor_status = "available"

        equity_rows = self._query(
            """
            WITH seeded(ts, equity) AS (
                SELECT -1 AS ts, ? AS equity
                UNION ALL
                SELECT ts, equity FROM paper_equity
                WHERE paper_account_id=? AND user_id=? AND equity IS NOT NULL
            ), running AS (
                SELECT ts, equity,
                       MAX(equity) OVER (
                           ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS peak
                FROM seeded
            )
            SELECT COALESCE(MAX(CASE WHEN peak > 0 THEN (peak - equity) / peak * 100 ELSE 0 END), 0)
                       AS max_drawdown,
                   MAX(CASE WHEN ts >= 0 THEN ts END) AS data_as_of,
                   SUM(CASE WHEN ts >= 0 THEN 1 ELSE 0 END) AS samples
            FROM running
            """,
            (start_balance, account_id, user_id),
        )
        equity_stats = equity_rows[0] if equity_rows else {}
        max_drawdown = _finite_number(equity_stats.get("max_drawdown"))
        data_as_of_ts = int(equity_stats.get("data_as_of") or 0)
        generated_at = datetime.now(UTC)
        data_as_of = datetime.fromtimestamp(data_as_of_ts, UTC) if data_as_of_ts else None
        stale = data_as_of is None or (generated_at - data_as_of).total_seconds() > 180
        started_ts = int(account.get("started_ts") or 0)
        period_start = datetime.fromtimestamp(started_ts, UTC) if started_ts else None

        month_rows = self._query(
            """
            SELECT closed_ts, COALESCE(pnl, 0) - COALESCE(fee, 0) AS net_pnl
            FROM paper_trades
            WHERE paper_account_id=? AND user_id=? AND closed_ts >= ? AND closed_ts < ?
            ORDER BY closed_ts
            """,
            (account_id, user_id, start_ts, end_ts),
        )
        calendar_days: dict[str, dict[str, Any]] = {}
        for row in month_rows:
            try:
                closed_at = datetime.fromtimestamp(int(row["closed_ts"]), UTC) + offset
            except (OSError, OverflowError, TypeError, ValueError):
                continue
            pnl = _finite_number(row.get("net_pnl"))
            date_key = closed_at.date().isoformat()
            day = calendar_days.setdefault(
                date_key,
                {
                    "date": date_key,
                    "pnl": 0.0,
                    "trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "breakeven": 0,
                },
            )
            day["pnl"] += pnl
            day["trades"] += 1
            if pnl > 0:
                day["wins"] += 1
            elif pnl < 0:
                day["losses"] += 1
            else:
                day["breakeven"] += 1
        days = []
        for day in calendar_days.values():
            day["pnl"] = round(day["pnl"], 2)
            days.append(day)

        equity = _finite_number(account.get("equity"))
        unrealized_pnl = _finite_number(account.get("upnl"))
        total_pnl = equity - start_balance
        return {
            "source": "paper_account",
            "scope": "user_account",
            "currency": "USDT",
            "generated_at": generated_at,
            "data_as_of": data_as_of,
            "period_start": period_start,
            "stale": stale,
            "metrics": {
                "total_pnl": round(total_pnl, 2),
                "total_return_pct": round(
                    total_pnl / start_balance * 100 if start_balance else 0, 2
                ),
                "realized_pnl": round(realized_pnl, 2),
                "unrealized_pnl": round(unrealized_pnl, 2),
                "win_rate": round(wins / (wins + losses) * 100 if wins + losses else 0, 1),
                "win_rate_basis": "decisive_trades",
                "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
                "profit_factor_status": profit_factor_status,
                "max_drawdown": round(max_drawdown, 2),
                "max_drawdown_basis": "since_reset_full_equity",
                "average_profit": round(realized_pnl / trades, 2) if trades else 0,
                "average_win": round(gross_profit / wins, 2) if wins else 0,
                "trades": trades,
                "wins": wins,
                "losses": losses,
                "breakeven": breakeven,
                "equity_samples": int(equity_stats.get("samples") or 0),
            },
            "calendar": {
                "month": month,
                "timezone_offset_minutes": timezone_offset_minutes,
                "timezone_label": _timezone_label(timezone_offset_minutes),
                "basis": "closed_trade_net_pnl",
                "total_pnl": round(sum(day["pnl"] for day in days), 2),
                "active_days": len(days),
                "days": days,
            },
        }

    def reset_paper(self, user_id: int, account_id: int) -> dict[str, Any]:
        with _REPORT_LOCK:
            from . import paper_engine

            self._configure_market_store()
            paper_engine.reset(user_id, account_id)
            return paper_engine.api_data(user_id, account_id)


def _finite_number(value: Any) -> float:
    try:
        numeric = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return numeric if math.isfinite(numeric) else 0.0


def _timezone_label(offset_minutes: int) -> str:
    sign = "+" if offset_minutes >= 0 else "-"
    hours, minutes = divmod(abs(offset_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"
