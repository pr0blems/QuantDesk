"""Narrow, auditable Binance USD-M futures trading adapter.

Only the endpoints required by the live strategy executor are exposed.  The
adapter deliberately supports standard USD-M accounts only; portfolio margin
needs a separate risk model and must not silently share these assumptions.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from http.client import HTTPSConnection
from typing import Any, Literal
from urllib.parse import urlsplit

from .binance_client import (
    MAX_RESPONSE_BYTES,
    BinanceAccountClientError,
    current_time_ms,
    signed_query,
)

TradeTransport = Callable[[str, str, dict[str, str], float], tuple[int, bytes]]
OrderSide = Literal["BUY", "SELL"]


@dataclass(frozen=True, slots=True)
class FuturesSymbolRules:
    symbol: str
    market_step_size: Decimal
    min_quantity: Decimal
    max_quantity: Decimal
    tick_size: Decimal
    min_notional: Decimal

    def quantity(self, raw: Decimal) -> Decimal:
        if not raw.is_finite() or raw <= 0:
            raise ValueError("quantity must be positive")
        steps = (raw / self.market_step_size).to_integral_value(rounding=ROUND_DOWN)
        value = steps * self.market_step_size
        if value < self.min_quantity or value > self.max_quantity:
            raise ValueError("quantity is outside Binance symbol limits")
        return value

    def price(self, raw: Decimal) -> Decimal:
        if not raw.is_finite() or raw <= 0:
            raise ValueError("price must be positive")
        ticks = (raw / self.tick_size).to_integral_value(rounding=ROUND_DOWN)
        value = ticks * self.tick_size
        if value <= 0:
            raise ValueError("price is below Binance tick size")
        return value


def _trade_transport(
    method: str, url: str, headers: dict[str, str], timeout: float
) -> tuple[int, bytes]:
    parsed = urlsplit(url)
    connection = HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    try:
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise BinanceAccountClientError("invalid_response")
        return response.status, body
    finally:
        connection.close()


class BinanceUsdMTradingClient:
    """Minimal signed USD-M client with bounded retries and symbol filtering."""

    def __init__(
        self,
        base_url: str,
        *,
        recv_window_ms: int = 5_000,
        timeout_seconds: float = 4.0,
        transport: TradeTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme.lower() != "https"
            or parsed.hostname not in {"fapi.binance.com", "demo-fapi.binance.com"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("unapproved Binance Futures base URL")
        if not 1_000 <= recv_window_ms <= 5_000:
            raise ValueError("recvWindow must be between 1000 and 5000 milliseconds")
        if not 1 <= timeout_seconds <= 10:
            raise ValueError("timeout must be between 1 and 10 seconds")
        self.base_url = base_url.rstrip("/")
        self.recv_window_ms = recv_window_ms
        self.timeout_seconds = timeout_seconds
        self.transport = transport or _trade_transport
        self._clock_offset_ms = 0
        self._rules_lock = threading.Lock()
        self._rules_expires_at = 0.0
        self._rules: dict[str, FuturesSymbolRules] = {}

    def symbol_rules(self, symbol: str) -> FuturesSymbolRules:
        normalized = self._symbol(symbol)
        now = time.monotonic()
        with self._rules_lock:
            if now >= self._rules_expires_at:
                self._rules = self._load_rules()
                self._rules_expires_at = now + 600
            rule = self._rules.get(normalized)
        if rule is None:
            raise BinanceAccountClientError("unsupported_symbol")
        return rule

    def position_mode(self, api_key: str, api_secret: str) -> Literal["one_way", "hedge"]:
        payload = self._signed("GET", "/fapi/v1/positionSide/dual", api_key, api_secret)
        if not isinstance(payload, dict) or not isinstance(payload.get("dualSidePosition"), bool):
            raise BinanceAccountClientError("invalid_response")
        return "hedge" if payload["dualSidePosition"] else "one_way"

    def change_leverage(
        self, api_key: str, api_secret: str, *, symbol: str, leverage: int
    ) -> dict[str, Any]:
        if isinstance(leverage, bool) or not 1 <= int(leverage) <= 20:
            raise ValueError("live leverage must be between 1 and 20")
        payload = self._signed(
            "POST",
            "/fapi/v1/leverage",
            api_key,
            api_secret,
            (("symbol", self._symbol(symbol)), ("leverage", int(leverage))),
        )
        return self._object(payload)

    def place_market_order(
        self,
        api_key: str,
        api_secret: str,
        *,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        client_order_id: str,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        params: list[tuple[str, str | int]] = [
            ("symbol", self._symbol(symbol)),
            ("side", self._side(side)),
            ("type", "MARKET"),
            ("quantity", self._decimal(quantity)),
            ("newClientOrderId", self._client_order_id(client_order_id)),
            ("newOrderRespType", "RESULT"),
        ]
        if reduce_only:
            params.append(("reduceOnly", "true"))
        return self._object(
            self._signed("POST", "/fapi/v1/order", api_key, api_secret, params)
        )

    def place_close_trigger(
        self,
        api_key: str,
        api_secret: str,
        *,
        symbol: str,
        side: OrderSide,
        order_type: Literal["STOP_MARKET", "TAKE_PROFIT_MARKET"],
        stop_price: Decimal,
        client_order_id: str,
    ) -> dict[str, Any]:
        if order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            raise ValueError("unsupported protective order type")
        return self._object(
            self._signed(
                "POST",
                "/fapi/v1/algoOrder",
                api_key,
                api_secret,
                (
                    ("algoType", "CONDITIONAL"),
                    ("symbol", self._symbol(symbol)),
                    ("side", self._side(side)),
                    ("type", order_type),
                    ("triggerPrice", self._decimal(stop_price)),
                    ("closePosition", "true"),
                    ("workingType", "MARK_PRICE"),
                    ("priceProtect", "true"),
                    ("clientAlgoId", self._client_order_id(client_order_id)),
                ),
            )
        )

    def query_algo_order(
        self, api_key: str, api_secret: str, *, client_order_id: str
    ) -> dict[str, Any]:
        return self._object(
            self._signed(
                "GET",
                "/fapi/v1/algoOrder",
                api_key,
                api_secret,
                (("clientAlgoId", self._client_order_id(client_order_id)),),
            )
        )

    def cancel_algo_order(
        self, api_key: str, api_secret: str, *, client_order_id: str
    ) -> dict[str, Any]:
        return self._object(
            self._signed(
                "DELETE",
                "/fapi/v1/algoOrder",
                api_key,
                api_secret,
                (("clientAlgoId", self._client_order_id(client_order_id)),),
            )
        )

    def query_order(
        self, api_key: str, api_secret: str, *, symbol: str, client_order_id: str
    ) -> dict[str, Any]:
        return self._object(
            self._signed(
                "GET",
                "/fapi/v1/order",
                api_key,
                api_secret,
                (
                    ("symbol", self._symbol(symbol)),
                    ("origClientOrderId", self._client_order_id(client_order_id)),
                ),
            )
        )

    def cancel_order(
        self, api_key: str, api_secret: str, *, symbol: str, client_order_id: str
    ) -> dict[str, Any]:
        return self._object(
            self._signed(
                "DELETE",
                "/fapi/v1/order",
                api_key,
                api_secret,
                (
                    ("symbol", self._symbol(symbol)),
                    ("origClientOrderId", self._client_order_id(client_order_id)),
                ),
            )
        )

    def _load_rules(self) -> dict[str, FuturesSymbolRules]:
        payload = self._public("/fapi/v1/exchangeInfo")
        if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
            raise BinanceAccountClientError("invalid_response")
        output: dict[str, FuturesSymbolRules] = {}
        for row in payload["symbols"]:
            if not isinstance(row, dict) or row.get("status") != "TRADING":
                continue
            symbol = row.get("symbol")
            filters = row.get("filters")
            if not isinstance(symbol, str) or not isinstance(filters, list):
                continue
            by_type = {
                item.get("filterType"): item
                for item in filters
                if isinstance(item, dict) and isinstance(item.get("filterType"), str)
            }
            lot = by_type.get("MARKET_LOT_SIZE") or by_type.get("LOT_SIZE")
            price_filter = by_type.get("PRICE_FILTER")
            notional_filter = by_type.get("MIN_NOTIONAL") or by_type.get("NOTIONAL") or {}
            if not isinstance(lot, dict) or not isinstance(price_filter, dict):
                continue
            try:
                output[symbol] = FuturesSymbolRules(
                    symbol=symbol,
                    market_step_size=self._positive_decimal(lot.get("stepSize")),
                    min_quantity=self._positive_decimal(lot.get("minQty")),
                    max_quantity=self._positive_decimal(lot.get("maxQty")),
                    tick_size=self._positive_decimal(price_filter.get("tickSize")),
                    min_notional=self._positive_decimal(
                        notional_filter.get("notional") or notional_filter.get("minNotional") or "5"
                    ),
                )
            except (TypeError, ValueError):
                continue
        return output

    def _public(self, path: str) -> dict[str, Any] | list[Any]:
        return self._request("GET", f"{self.base_url}{path}", {})

    def _signed(
        self,
        method: str,
        path: str,
        api_key: str,
        api_secret: str,
        params: Iterable[tuple[str, str | int]] = (),
    ) -> dict[str, Any] | list[Any]:
        if not api_key or not api_secret:
            raise BinanceAccountClientError("credential_error")
        for attempt in range(2):
            timestamp = current_time_ms() + self._clock_offset_ms
            query = signed_query(api_secret, self.recv_window_ms, timestamp, params)
            try:
                return self._request(
                    method,
                    f"{self.base_url}{path}?{query}",
                    {"X-MBX-APIKEY": api_key},
                )
            except BinanceAccountClientError as exc:
                if exc.code == -1021 and attempt == 0:
                    self._sync_clock()
                    continue
                raise
        raise BinanceAccountClientError("timestamp", code=-1021)

    def _sync_clock(self) -> None:
        started = current_time_ms()
        payload = self._public("/fapi/v1/time")
        finished = current_time_ms()
        if not isinstance(payload, dict):
            raise BinanceAccountClientError("invalid_response")
        try:
            server_time = int(payload["serverTime"])
        except (KeyError, TypeError, ValueError):
            raise BinanceAccountClientError("invalid_response") from None
        self._clock_offset_ms = server_time - (started + (finished - started) // 2)

    def _request(
        self, method: str, url: str, headers: dict[str, str]
    ) -> dict[str, Any] | list[Any]:
        request_headers = {"Accept": "application/json", **headers}
        try:
            status, body = self.transport(method, url, request_headers, self.timeout_seconds)
        except BinanceAccountClientError:
            raise
        except TimeoutError:
            raise BinanceAccountClientError("timeout") from None
        except OSError:
            raise BinanceAccountClientError("network") from None
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            raise BinanceAccountClientError("invalid_response") from None
        error = payload if isinstance(payload, dict) else {}
        try:
            code = int(error["code"]) if "code" in error else None
        except (TypeError, ValueError):
            code = None
        if not 200 <= status < 300 or (code is not None and code < 0):
            category = "rejected"
            if code in {-1002, -2014, -2015, -2017, -1022} or status == 401:
                category = "authentication"
            elif code == -1021:
                category = "timestamp"
            elif code == -1003 or status in {418, 429}:
                category = "rate_limit"
            elif status == 408:
                category = "timeout"
            elif status >= 500:
                category = "upstream"
            raise BinanceAccountClientError(category, code=code)
        if not isinstance(payload, (dict, list)):
            raise BinanceAccountClientError("invalid_response")
        return payload

    @staticmethod
    def _object(payload: dict[str, Any] | list[Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise BinanceAccountClientError("invalid_response")
        return payload

    @staticmethod
    def _positive_decimal(value: Any) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("invalid decimal") from None
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError("invalid positive decimal")
        return parsed

    @staticmethod
    def _decimal(value: Decimal) -> str:
        if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
            raise ValueError("invalid positive decimal")
        return format(value, "f")

    @staticmethod
    def _symbol(value: str) -> str:
        normalized = str(value).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3,32}", normalized):
            raise ValueError("invalid Binance symbol")
        return normalized

    @staticmethod
    def _side(value: str) -> OrderSide:
        normalized = str(value).upper()
        if normalized not in {"BUY", "SELL"}:
            raise ValueError("invalid Binance order side")
        return normalized  # type: ignore[return-value]

    @staticmethod
    def _client_order_id(value: str) -> str:
        normalized = str(value).strip()
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,36}", normalized):
            raise ValueError("invalid Binance client order id")
        return normalized
