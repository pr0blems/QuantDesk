from __future__ import annotations

import json
import math
import threading
import time
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
        part + (f":p{index}" if index < len(params) else "")
        for index, part in enumerate(parts)
    )
    return text(statement), {f"p{index}": value for index, value in enumerate(params)}


_REPORT_LOCK = threading.Lock()


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

    def _configure_market_store(self) -> None:
        """Point the in-process market modules at the shared MySQL engine."""
        from . import market_store

        market_store.configure_engine(self.engine)

    def overview(self, watchlist: list[str]) -> dict[str, Any]:
        tickers = {row["symbol"]: row for row in self._query("SELECT * FROM ticker")}
        score_rows = self._query(
            """
            SELECT s.symbol, s.tf, s.score FROM scores s
            JOIN (SELECT symbol, tf, MAX(open_time) mo FROM scores GROUP BY symbol, tf) m
            ON s.symbol=m.symbol AND s.tf=m.tf AND s.open_time=m.mo
            """
        )
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
            horizon = horizon_names.get(
                int(row["horizon_seconds"]), str(row["horizon_seconds"])
            )
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
            row["symbol"]: _opportunity_out(row, compact=True)
            for row in opportunity_rows
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
        selected = set(watchlist)
        weights = {"15m": 0.3, "1h": 0.4, "4h": 0.3}
        items = []
        latest = 0
        for symbol in self.symbols:
            ticker = tickers.get(symbol, {})
            latest = max(latest, int(ticker.get("ts") or 0))
            tf_scores = scores.get(symbol, {})
            numerator = sum(
                tf_scores.get(tf, 0) * weight for tf, weight in weights.items() if tf in tf_scores
            )
            denominator = sum(weight for tf, weight in weights.items() if tf in tf_scores)
            base = symbol.replace("USDT", "").replace("USD1", "")
            items.append(
                {
                    "symbol": symbol,
                    "underlying": metadata.get(symbol, {}).get("underlyingType", ""),
                    "price": _finite_number(ticker.get("price")),
                    "pct_2m": None,
                    "pct_5m": None,
                    "pct_10m": None,
                    "pct_24h": _finite_number(ticker.get("pct_24h")),
                    "quote_volume": _finite_number(ticker.get("quote_volume")),
                    "bid_depth_notional": None,
                    "ask_depth_notional": None,
                    "book_imbalance": None,
                    "book_imbalance_5": None,
                    "depth_levels": 0,
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
        rows = self._query(
            """
            SELECT s.symbol, s.score FROM scores s
            JOIN (SELECT symbol, MAX(open_time) mo FROM scores WHERE tf='1h' GROUP BY symbol) m
            ON s.symbol=m.symbol AND s.open_time=m.mo WHERE s.tf='1h'
            """
        )
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
        ticker_rows = self._query(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN ts>=? THEN 1 ELSE 0 END) fresh
               FROM ticker""",
            (now_seconds - 300,),
        )
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
        ticker = ticker_rows[0] if ticker_rows else {}
        lifecycle = lifecycle_rows[0] if lifecycle_rows else {}
        outcomes = outcome_rows[0] if outcome_rows else {}
        total_symbols = len(self.symbols)
        fresh = int(ticker.get("fresh") or 0)
        return {
            "market_data": {
                "symbols": total_symbols,
                "fresh_microstructure": fresh,
                "coverage_pct": round(fresh / total_symbols * 100, 2)
                if total_symbols
                else 0,
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

    def prediction_history(self, page: int, page_size: int = 50) -> dict[str, Any]:
        """Return one newest-first page of battle predictions and their labels."""
        total_rows = self._query("SELECT COUNT(*) total FROM battle_predictions")
        total = int(total_rows[0].get("total") or 0) if total_rows else 0
        pages = max(1, math.ceil(total / page_size))
        current_page = min(max(1, page), pages)
        offset = (current_page - 1) * page_size
        rows = self._query(
            """SELECT p.public_id,p.symbol,p.horizon_seconds,p.prediction_state,
                      p.result AS prediction_result,p.battle_score,p.long_probability,
                      p.short_probability,p.neutral_probability,p.confidence_score,
                      p.confidence_label,p.gross_edge_bps,p.entry_price,p.spread_bps,
                      p.target_bps,p.stop_bps,p.model_key,p.model_version,
                      p.predicted_at_ms,p.valid_until_ms,
                      o.status,o.actual_result,o.exit_price,o.raw_return_bps,
                      o.directional_return_bps,o.max_favorable_bps,o.max_adverse_bps,
                      o.hit_result,o.cost_bps,o.due_at_ms,o.completed_at_ms
               FROM battle_predictions p
               JOIN prediction_outcomes o ON o.prediction_id=p.id
               ORDER BY p.predicted_at_ms DESC,p.id DESC
               LIMIT ? OFFSET ?""",
            (page_size, offset),
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
        }

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
