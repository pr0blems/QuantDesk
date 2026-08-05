from __future__ import annotations

import json
import math
import re
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
        part + (f":p{index}" if index < len(params) else "") for index, part in enumerate(parts)
    )
    return text(statement), {f"p{index}": value for index, value in enumerate(params)}


_REPORT_LOCK = threading.Lock()
_MONITOR_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{3,32}$")

# Fallback labels for temporary or externally supplied symbol configurations.
# Production contracts use the full-symbol catalogue in
# ``config/contract_annotations.json`` so every displayed label is reviewed
# rather than inferred from a short ticker.
_CONTRACT_ANNOTATIONS = {
    "AAOI": "Applied Optoelectronics",
    "AAPL": "Apple",
    "ADBE": "Adobe",
    "ALAB": "Astera Labs",
    "AMAT": "Applied Materials",
    "AMD": "AMD",
    "AMZN": "Amazon",
    "ANTHROPIC": "Anthropic",
    "APP": "AppLovin",
    "ARM": "Arm Holdings",
    "ASML": "ASML 光刻机",
    "ASTS": "AST SpaceMobile",
    "AVGO": "Broadcom",
    "AXTI": "AXT 半导体材料",
    "BABA": "阿里巴巴",
    "BBX": "BBX 标的",
    "BE": "Bloom Energy",
    "BITO": "比特币期货 ETF",
    "BMNR": "BitMine Immersion",
    "BNC": "BNC 标的",
    "BOT": "BOT 标的",
    "BRKB": "伯克希尔哈撒韦 B",
    "BSP": "BSP 标的",
    "BX": "Blackstone",
    "BZ": "BOSS 直聘",
    "CAT": "卡特彼勒",
    "CBRS": "Cerebras Systems",
    "CIEN": "Ciena 光通信",
    "CL": "WTI 原油",
    "COHR": "Coherent 光学",
    "COIN": "Coinbase",
    "COPPER": "铜",
    "COST": "Costco",
    "CRC": "California Resources",
    "CRDO": "Credo Technology",
    "CRM": "Salesforce",
    "CRWD": "CrowdStrike",
    "CRWV": "CoreWeave",
    "CSCO": "Cisco",
    "DELL": "戴尔科技",
    "DIS": "迪士尼",
    "DKNG": "DraftKings",
    "DRAM": "DRAM 存储芯片",
    "EBAY": "eBay",
    "EWJ": "日本股票 ETF",
    "EWT": "台湾股票 ETF",
    "EWY": "韩国股票 ETF",
    "EWZ": "巴西股票 ETF",
    "FLEX": "Flex",
    "FLNC": "Fluence Energy",
    "FWDI": "FWDI 标的",
    "GEV": "GE Vernova",
    "GIGADEV": "兆易创新",
    "GLW": "康宁",
    "GME": "GameStop",
    "GOOGL": "Alphabet A",
    "GS": "高盛",
    "HD": "家得宝",
    "HIMS": "Hims & Hers",
    "HK0700": "腾讯控股",
    "HK1810": "小米集团",
    "HOOD": "Robinhood",
    "HPE": "慧与科技",
    "HYUNDAI": "现代汽车",
    "IBM": "IBM",
    "INTC": "英特尔",
    "INTW": "INTW 标的",
    "IREN": "IREN 比特币矿企",
    "IWM": "罗素 2000 ETF",
    "JPM": "摩根大通",
    "KLAC": "KLA 半导体设备",
    "KORU": "韩国 3 倍做多 ETF",
    "KSTR": "KSTR 标的",
    "LITE": "Lumentum",
    "LLY": "礼来",
    "LRCX": "拉姆研究",
    "META": "Meta",
    "MINIMAX": "MiniMax",
    "MRVL": "Marvell Technology",
    "MSFT": "微软",
    "MSTR": "Strategy（原 MicroStrategy）",
    "MU": "美光科技",
    "MUU": "MUU 标的",
    "MVLL": "Marvell 关联标的",
    "NATGAS": "天然气",
    "NBIS": "Nebius Group",
    "NFLX": "Netflix",
    "NOK": "诺基亚",
    "NOW": "ServiceNow",
    "NVDA": "英伟达",
    "NVO": "诺和诺德",
    "ONDS": "Ondas Holdings",
    "OPENAI": "OpenAI",
    "ORCL": "甲骨文",
    "PANW": "Palo Alto Networks",
    "PAYP": "PayPal",
    "PENG": "Penguin Solutions",
    "PLTR": "Palantir",
    "POPMART": "泡泡玛特",
    "PYPL": "PayPal",
    "QCOM": "高通",
    "QNTX": "QNTX 标的",
    "QQQ": "纳斯达克 100 ETF",
    "RIVN": "Rivian",
    "RKLB": "Rocket Lab",
    "SAMSUNG": "三星电子",
    "SHAZ": "SHAZ 标的",
    "SKHYNIX": "SK 海力士",
    "SKHY": "SK 海力士",
    "SMCI": "超微电脑",
    "SMH": "半导体 ETF",
    "SNDK": "闪迪",
    "SNOW": "Snowflake",
    "SNXX": "SNXX 标的",
    "SOFI": "SoFi",
    "SONY": "索尼",
    "SOXL": "半导体 3 倍做多 ETF",
    "SOXS": "半导体 3 倍做空 ETF",
    "SPC": "SpaceX",
    "SPCX": "SpaceX",
    "SPY": "标普 500 ETF",
    "SQQQ": "纳指 100 3 倍做空 ETF",
    "STR": "Strategy",
    "STXX": "欧洲斯托克 50",
    "TBT": "20 年美债 2 倍做空 ETF",
    "TENCENT": "腾讯控股",
    "TER": "泰瑞达",
    "TMF": "20 年美债 3 倍做多 ETF",
    "TQQQ": "纳指 100 3 倍做多 ETF",
    "TSLA": "特斯拉",
    "TSM": "台积电",
    "TTWO": "Take-Two Interactive",
    "TXN": "德州仪器",
    "TZA": "小盘股 3 倍做空 ETF",
    "UBER": "优步",
    "URNM": "铀矿 ETF",
    "USAR": "USA Rare Earth",
    "UVXY": "波动率 1.5 倍 ETF",
    "V": "Visa",
    "WDC": "西部数据",
    "WEN": "Wendy's",
    "WMT": "沃尔玛",
    "XAG": "白银",
    "XAU": "黄金",
    "XBI": "生物科技 ETF",
    "XLE": "能源 ETF",
    "XPD": "钯金",
    "XPT": "铂金",
    "ZHIPU": "智谱 AI",
    "ZM": "Zoom",
    "BTC": "比特币",
    "ETH": "以太坊",
    "BNB": "BNB",
    "SOL": "Solana",
    "XRP": "瑞波币",
    "DOGE": "狗狗币",
}


def _contract_annotation(symbol: str, metadata: dict[str, Any]) -> str:
    configured = metadata.get("annotation") or metadata.get("name")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    base = symbol.removesuffix("USDT").removesuffix("USD1")
    return _CONTRACT_ANNOTATIONS.get(base, f"{base}（未配置合约说明）")


def _load_contract_annotations(path: Path) -> dict[str, str]:
    """Load the curated, full-symbol annotation catalogue when it is present.

    The exchange's ``underlyingType`` is intentionally broad, so keeping this
    separate from the exchange-discovered symbols lets us state the real
    company, fund, index, commodity, or pre-IPO reference without modifying
    the source list every time Binance adds a contract.
    """
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        symbol.strip().upper(): annotation.strip()
        for symbol, annotation in raw.items()
        if isinstance(symbol, str)
        and _MONITOR_SYMBOL_PATTERN.fullmatch(symbol.strip().upper())
        and isinstance(annotation, str)
        and annotation.strip()
    }


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


def _opportunity_out(row: dict[str, Any], *, compact: bool = False) -> dict[str, Any]:
    evidence = _json_object(row.get("evidence_json"))
    result = {
        "id": row.get("public_id"),
        "symbol": row.get("symbol"),
        "direction": row.get("direction"),
        "status": row.get("status"),
        "quality_score": _finite_number(row.get("quality_score")),
        "expected_value_score": _finite_number(row.get("expected_value_score")),
        "entry_price": _finite_number(row.get("entry_price")),
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
        annotations_path = self.symbols_config.with_name("contract_annotations.json")
        self.contract_annotations = _load_contract_annotations(annotations_path)
        if annotations_path.is_file():
            missing_annotations = self.symbol_set - set(self.contract_annotations)
            extra_annotations = set(self.contract_annotations) - self.symbol_set
            if missing_annotations or extra_annotations:
                raise MonitorUnavailable("contract monitor annotation catalog is incomplete")

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            statement, values = _bind_params(sql, params)
            with self.engine.connect() as connection:
                return [dict(row) for row in connection.execute(statement, values).mappings()]
        except SQLAlchemyError as exc:
            raise MonitorUnavailable("contract monitor data query failed") from exc

    def _watchlist_symbols(self, watchlist: list[str] | tuple[str, ...]) -> list[str]:
        symbols: list[str] = []
        for raw_symbol in watchlist:
            if not isinstance(raw_symbol, str):
                continue
            symbol = raw_symbol.strip().upper()
            if _MONITOR_SYMBOL_PATTERN.fullmatch(symbol) and symbol not in symbols:
                symbols.append(symbol)
        return symbols

    def _validate_symbol(
        self, symbol: str, extra_symbols: list[str] | tuple[str, ...] = ()
    ) -> str:
        normalized = symbol.strip().upper()
        if (
            normalized not in self.symbol_set
            and normalized not in self._watchlist_symbols(extra_symbols)
        ):
            raise MonitorUnavailable("unknown contract monitor symbol")
        return normalized

    def _configure_market_store(self) -> None:
        """Point the in-process market modules at the shared MySQL engine."""
        from quantdesk import store as market_store

        market_store.configure_engine(self.engine)

    def overview(self, watchlist: list[str], user_id: int | None = None) -> dict[str, Any]:
        tickers = {row["symbol"]: row for row in self._query("SELECT * FROM ticker")}
        now_seconds = int(time.time())
        price_change_windows = {120: {}, 300: {}, 600: {}}
        snapshot_rows = self._query(
            """SELECT symbol,bucket_ts,price FROM contract_price_snapshots
               WHERE bucket_ts BETWEEN ? AND ?""",
            (now_seconds - 11 * 60, now_seconds - 2 * 60),
        )
        for row in snapshot_rows:
            symbol = str(row["symbol"])
            bucket_ts = int(row["bucket_ts"])
            for window_seconds, references in price_change_windows.items():
                lower_bound = now_seconds - window_seconds - 60
                upper_bound = now_seconds - window_seconds
                existing = references.get(symbol)
                if lower_bound <= bucket_ts <= upper_bound and (
                    existing is None or bucket_ts > int(existing["bucket_ts"])
                ):
                    references[symbol] = row
        movement_rows = self._query(
            """SELECT symbol,SUM(up_count) AS up_count,SUM(down_count) AS down_count
               FROM contract_price_move_buckets WHERE bucket_ts>=? GROUP BY symbol""",
            (int(time.time()) - 30 * 60,),
        )
        movements = {row["symbol"]: row for row in movement_rows}
        depth_rows = self._query(
            """SELECT symbol,bid_depth_notional,ask_depth_notional,book_imbalance,
                      book_imbalance_5,depth_levels,received_at
               FROM market_microstructure"""
        )
        depth_by_symbol = {str(row["symbol"]): row for row in depth_rows}
        underlying_rows = self._query("SELECT * FROM underlying_market_quotes")
        underlying_by_symbol = {
            str(row["contract_symbol"]): row for row in underlying_rows
        }
        battle_rows = self._query(
            """SELECT p.*,f.quality_score FROM battle_predictions p
               JOIN prediction_feature_snapshots f ON f.id=p.feature_snapshot_id
               WHERE p.current_marker=1 ORDER BY p.symbol,p.horizon_seconds"""
        )
        commission_rows = (
            self._query(
                "SELECT symbol,taker_rate FROM binance_user_commission_rates WHERE user_id=?",
                (user_id,),
            )
            if user_id is not None
            else []
        )
        commissions = {
            str(row["symbol"]): float(row["taker_rate"]) for row in commission_rows
        }
        horizon_names = {300: "5m", 900: "15m", 3_600: "1h", 7_200: "2h"}
        battles: dict[str, dict[str, dict[str, Any]]] = {}
        now_ms = int(time.time() * 1_000)
        for row in battle_rows:
            symbol = str(row["symbol"])
            spread = max(0.0, float(row.get("spread_bps") or 0))
            taker_rate = commissions.get(symbol)
            cost_bps = (
                spread + max(0.5, spread * 0.15) + taker_rate * 20_000
                if taker_rate is not None
                else None
            )
            gross_edge = _finite_number(row.get("gross_edge_bps"))
            after_cost = (
                round(float(gross_edge) - cost_bps, 4)
                if gross_edge is not None and cost_bps is not None
                else None
            )
            horizon = horizon_names.get(int(row["horizon_seconds"]), str(row["horizon_seconds"]))
            battles.setdefault(symbol, {})[horizon] = {
                "id": row.get("public_id"),
                "model_key": row.get("model_key"),
                "model_version": int(row.get("model_version") or 0),
                "horizon_seconds": int(row["horizon_seconds"]),
                "state": row.get("prediction_state"),
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
                "gross_edge_bps": gross_edge,
                "estimated_cost_bps": round(cost_bps, 4) if cost_bps is not None else None,
                "edge_after_cost_bps": after_cost,
                "fee_source": "binance_user_commission" if taker_rate is not None else "unavailable",
                "reason_codes": _json_array(row.get("reason_codes_json")),
                "components": _json_object(row.get("components_json")),
                "predicted_at_ms": int(row.get("predicted_at_ms") or 0),
                "valid_until_ms": int(row.get("valid_until_ms") or 0),
                "stale": now_ms > int(row.get("valid_until_ms") or 0),
                "execution_allowed": False,
            }
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
        selected = set(self._watchlist_symbols(watchlist))
        symbols = [*self.symbols]
        symbols.extend(symbol for symbol in selected if symbol not in self.symbol_set)
        weights = {"15m": 0.3, "1h": 0.4, "4h": 0.3}
        items = []
        latest = 0
        for symbol in symbols:
            ticker = tickers.get(symbol, {})
            latest = max(latest, int(ticker.get("ts") or 0))
            price = _finite_number(ticker.get("price"))
            price_changes = {}
            for window_seconds, references in price_change_windows.items():
                reference_price = _finite_number(references.get(symbol, {}).get("price"))
                price_changes[window_seconds] = (
                    round((price - reference_price) / reference_price * 100, 4)
                    if price is not None and reference_price is not None and reference_price > 0
                    else None
            )
            tf_scores = scores.get(symbol, {})
            depth = depth_by_symbol.get(symbol, {})
            numerator = sum(
                tf_scores.get(tf, 0) * weight for tf, weight in weights.items() if tf in tf_scores
            )
            denominator = sum(weight for tf, weight in weights.items() if tf in tf_scores)
            base = symbol.replace("USDT", "").replace("USD1", "")
            symbol_metadata = metadata.get(symbol, {})
            underlying_quote = underlying_by_symbol.get(symbol, {})
            underlying_status = str(underlying_quote.get("status") or "pending")
            underlying_has_market_data = bool(underlying_quote.get("quote_symbol")) and (
                underlying_status in {"ok", "stale"}
            )
            contract_time_ms = int(ticker.get("ts") or 0)
            if contract_time_ms and contract_time_ms < 100_000_000_000:
                contract_time_ms *= 1_000
            underlying_time_ms = int(underlying_quote.get("market_time_ms") or 0)
            alignment_delta_ms = (
                abs(contract_time_ms - underlying_time_ms)
                if contract_time_ms and underlying_time_ms
                else None
            )
            if not underlying_has_market_data or not contract_time_ms:
                alignment_status = "unavailable"
            elif str(underlying_quote.get("market_state") or "") == "closed":
                alignment_status = "closed"
            elif underlying_status == "stale":
                alignment_status = "stale"
            elif (
                alignment_delta_ms is not None
                and contract_time_ms // 60_000 == underlying_time_ms // 60_000
            ):
                alignment_status = "aligned"
            else:
                alignment_status = "lagging"
            underlying_price = (
                _optional_finite_number(underlying_quote.get("price"))
                if underlying_has_market_data
                else None
            )
            underlying_currency = (
                str(underlying_quote.get("currency") or "")
                if underlying_has_market_data
                else ""
            )
            basis_comparable = (
                price is not None
                and underlying_price is not None
                and underlying_price > 0
                and underlying_currency == "USD"
                and str(underlying_quote.get("relation") or "")
                in {"direct", "benchmark"}
                and alignment_status == "aligned"
                and underlying_status == "ok"
            )
            basis_bps = (
                round((price / underlying_price - 1) * 10_000, 2)
                if basis_comparable
                else None
            )
            spread_alert = (
                "strong"
                if basis_bps is not None and abs(basis_bps) >= 50
                else "watch"
                if basis_bps is not None and abs(basis_bps) >= 25
                else "normal"
                if basis_bps is not None
                else "disabled"
            )
            items.append(
                {
                    "symbol": symbol,
                    "annotation": self.contract_annotations.get(symbol)
                    or _contract_annotation(symbol, symbol_metadata),
                    "underlying": symbol_metadata.get("underlyingType", ""),
                    "underlying_quote": {
                        "quote_symbol": underlying_quote.get("quote_symbol"),
                        "relation": underlying_quote.get("relation"),
                        "instrument_type": underlying_quote.get("instrument_type"),
                        "display_name": underlying_quote.get("display_name")
                        if underlying_has_market_data
                        else None,
                        "source": underlying_quote.get("source"),
                        "status": underlying_status,
                        "market_state": (
                            underlying_quote.get("market_state") or "unknown"
                            if underlying_has_market_data
                            else "unavailable"
                        ),
                        "currency": underlying_currency or None,
                        "exchange_name": underlying_quote.get("exchange_name")
                        if underlying_has_market_data
                        else None,
                        "price": underlying_price,
                        "previous_close": (
                            _optional_finite_number(underlying_quote.get("previous_close"))
                            if underlying_has_market_data
                            else None
                        ),
                        "change_pct": (
                            _optional_finite_number(underlying_quote.get("change_pct"))
                            if underlying_has_market_data
                            else None
                        ),
                        "pct_2m": (
                            _optional_finite_number(underlying_quote.get("pct_2m"))
                            if underlying_has_market_data
                            else None
                        ),
                        "pct_5m": (
                            _optional_finite_number(underlying_quote.get("pct_5m"))
                            if underlying_has_market_data
                            else None
                        ),
                        "pct_10m": (
                            _optional_finite_number(underlying_quote.get("pct_10m"))
                            if underlying_has_market_data
                            else None
                        ),
                        "pct_24h": (
                            _optional_finite_number(underlying_quote.get("pct_24h"))
                            if underlying_has_market_data
                            else None
                        ),
                        "regular_market_price": (
                            _optional_finite_number(
                                underlying_quote.get("regular_market_price")
                            )
                            if underlying_has_market_data
                            else None
                        ),
                        "day_open": (
                            _optional_finite_number(underlying_quote.get("day_open"))
                            if underlying_has_market_data
                            else None
                        ),
                        "day_high": (
                            _optional_finite_number(underlying_quote.get("day_high"))
                            if underlying_has_market_data
                            else None
                        ),
                        "day_low": (
                            _optional_finite_number(underlying_quote.get("day_low"))
                            if underlying_has_market_data
                            else None
                        ),
                        "volume": (
                            _optional_finite_number(underlying_quote.get("volume"))
                            if underlying_has_market_data
                            else None
                        ),
                        "market_time_ms": (
                            int(underlying_quote.get("market_time_ms") or 0)
                            if underlying_has_market_data
                            else 0
                        ),
                        "received_at_ms": int(
                            underlying_quote.get("received_at_ms") or 0
                        ),
                        "basis_bps": basis_bps,
                        "basis_comparable": basis_comparable,
                        "spread_alert": spread_alert,
                        "contract_time_ms": contract_time_ms,
                        "alignment_delta_ms": alignment_delta_ms,
                        "alignment_status": alignment_status,
                        "stale": underlying_status != "ok",
                    },
                    "price": price,
                    "pct_2m": price_changes[120],
                    "pct_5m": price_changes[300],
                    "pct_10m": price_changes[600],
                    "pct_24h": _finite_number(ticker.get("pct_24h")),
                    "quote_volume": _finite_number(ticker.get("quote_volume")),
                    "bid_depth_notional": _finite_number(depth.get("bid_depth_notional")),
                    "ask_depth_notional": _finite_number(depth.get("ask_depth_notional")),
                    "book_imbalance": _finite_number(depth.get("book_imbalance")),
                    "book_imbalance_5": _finite_number(depth.get("book_imbalance_5")),
                    "depth_levels": int(depth.get("depth_levels") or 0),
                    "depth_updated_at": int(depth.get("received_at") or 0),
                    "score": round(numerator / denominator) if denominator else None,
                    "tf_scores": tf_scores,
                    "watch": symbol in selected,
                    "position": None,
                    "trending": base in trending,
                    "opportunity": opportunities.get(symbol),
                    "green_flashes_30m": int(movements.get(symbol, {}).get("up_count") or 0),
                    "red_flashes_30m": int(movements.get(symbol, {}).get("down_count") or 0),
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
        """Return measurable discovery, data-quality and shadow-execution feedback."""
        now_ms = int(time.time() * 1_000)
        coverage_rows = self._query(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN received_at>=? THEN 1 ELSE 0 END) fresh,
                      AVG(CASE WHEN received_at>=? THEN spread_bps END) avg_spread_bps
               FROM market_microstructure""",
            (now_ms - 15_000, now_ms - 15_000),
        )
        lifecycle_rows = self._query(
            """SELECT COUNT(*) active,
                      SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) confirmed,
                      COUNT(DISTINCT scanner_key) scanners
               FROM market_opportunities WHERE current_marker=1"""
        )
        outcome_rows = self._query(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) completed,
                      SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) pending,
                      AVG(CASE WHEN status='completed' THEN directional_return_bps END) avg_return_bps,
                      AVG(CASE WHEN status='completed' THEN directional_return_bps>0 ELSE NULL END) hit_rate
               FROM opportunity_outcomes"""
        )
        scanner_rows = self._query(
            """SELECT o.scanner_key,x.horizon_seconds,COUNT(*) samples,
                      AVG(x.directional_return_bps) avg_return_bps,
                      AVG(CASE WHEN x.directional_return_bps>0 THEN 1 ELSE 0 END) hit_rate,
                      AVG(x.max_favorable_bps) avg_mfe_bps,AVG(x.max_adverse_bps) avg_mae_bps
               FROM opportunity_outcomes x
               JOIN market_opportunities o ON o.id=x.opportunity_id
               WHERE x.status='completed'
               GROUP BY o.scanner_key,x.horizon_seconds
               ORDER BY x.horizon_seconds,o.scanner_key"""
        )
        quality_rows = self._query(
            "SELECT COUNT(*) events FROM market_data_quality_events WHERE event_time>=?",
            (now_ms - 86_400_000,),
        )
        shadow_rows = self._query(
            """SELECT COUNT(*) intents,
                      SUM(CASE WHEN state='filled' THEN 1 ELSE 0 END) filled,
                      SUM(CASE WHEN state='risk_rejected' THEN 1 ELSE 0 END) rejected
               FROM order_intents"""
        )
        coverage = coverage_rows[0] if coverage_rows else {}
        lifecycle = lifecycle_rows[0] if lifecycle_rows else {}
        outcomes = outcome_rows[0] if outcome_rows else {}
        shadow = shadow_rows[0] if shadow_rows else {}
        total_symbols = len(self.symbols)
        fresh = int(coverage.get("fresh") or 0)
        return {
            "market_data": {
                "symbols": total_symbols,
                "fresh_microstructure": fresh,
                "coverage_pct": round(fresh / total_symbols * 100, 2) if total_symbols else 0,
                "avg_spread_bps": _finite_number(coverage.get("avg_spread_bps")),
                "quality_events_24h": int(
                    (quality_rows[0] if quality_rows else {}).get("events") or 0
                ),
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
                "intents": int(shadow.get("intents") or 0),
                "filled": int(shadow.get("filled") or 0),
                "rejected": int(shadow.get("rejected") or 0),
                "live_locked": True,
            },
            "targets": {
                "microstructure_coverage_pct": 99.0,
                "candidate_label_coverage_pct": 100.0,
                "notice": "指标为工程验收目标，不代表收益承诺。",
            },
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
        rows = self._query(
            """SELECT n.*,nd.source_tier,c.public_id event_id,c.state verification_state,
                      c.independent_origins,c.quality_score,d.symbol,d.direction,
                      d.truth_confidence,d.impact_confidence,d.market_confirmation,
                      d.reference_status,d.counterevidence_json,d.valid_until
               FROM news n
               LEFT JOIN news_documents nd ON nd.news_id=n.id
               LEFT JOIN news_event_clusters c ON c.id=nd.event_id
               LEFT JOIN news_decisions d ON d.event_id=c.id AND d.current_marker=1
               ORDER BY n.ts DESC LIMIT ?""",
            (limit * 5,),
        )
        output: list[dict[str, Any]] = []
        indexed: dict[str, dict[str, Any]] = {}
        for source_row in rows:
            row = dict(source_row)
            news_id = str(row["id"])
            if news_id not in indexed and len(output) >= limit:
                break
            assessment = None
            if row.get("symbol"):
                counterevidence = row.get("counterevidence_json")
                if isinstance(counterevidence, str):
                    try:
                        counterevidence = json.loads(counterevidence)
                    except (TypeError, ValueError):
                        counterevidence = []
                assessment = {
                    "symbol": row.pop("symbol"),
                    "direction": row.pop("direction"),
                    "truth_confidence": _finite_number(row.pop("truth_confidence")),
                    "impact_confidence": _finite_number(row.pop("impact_confidence")),
                    "market_confirmation": row.pop("market_confirmation"),
                    "reference_status": row.pop("reference_status"),
                    "counterevidence": counterevidence or [],
                    "valid_until": row.pop("valid_until"),
                }
            row.pop("counterevidence_json", None)
            if news_id not in indexed:
                row["assessments"] = []
                indexed[news_id] = row
                output.append(row)
            if assessment:
                indexed[news_id]["assessments"].append(assessment)
        return output[:limit]

    def klines(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        extra_symbols: list[str] | tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        normalized = self._validate_symbol(symbol, extra_symbols)
        rows = self._query(
            """
            SELECT open_time, open, high, low, close, volume FROM klines
            WHERE symbol=? AND tf=? ORDER BY open_time DESC LIMIT ?
            """,
            (normalized, timeframe, limit),
        )
        return list(reversed(rows))

    def score_detail(
        self, symbol: str, extra_symbols: list[str] | tuple[str, ...] = ()
    ) -> dict[str, Any]:
        normalized = self._validate_symbol(symbol, extra_symbols)
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

    def report(
        self, symbol: str, extra_symbols: list[str] | tuple[str, ...] = ()
    ) -> dict[str, Any]:
        normalized = self._validate_symbol(symbol, extra_symbols)
        with _REPORT_LOCK:
            from quantdesk import report as market_report

            self._configure_market_store()
            return market_report.build_report(normalized)

    def paper(
        self, user_id: int, account_id: int, timezone_offset_minutes: int = 0
    ) -> dict[str, Any]:
        with _REPORT_LOCK:
            from quantdesk import paper as paper_engine

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
            from quantdesk import paper as paper_engine

            self._configure_market_store()
            paper_engine.reset(user_id, account_id)
            return paper_engine.api_data(user_id, account_id)


def _finite_number(value: Any) -> float:
    try:
        numeric = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return numeric if math.isfinite(numeric) else 0.0


def _optional_finite_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _timezone_label(offset_minutes: int) -> str:
    sign = "+" if offset_minutes >= 0 else "-"
    hours, minutes = divmod(abs(offset_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"
