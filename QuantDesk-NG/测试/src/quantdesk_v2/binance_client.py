from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from http.client import HTTPSConnection
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

MAX_RESPONSE_BYTES = 512 * 1024
FUTURES_ACCOUNT_PATH = "/fapi/v3/account"
PORTFOLIO_ACCOUNT_PATH = "/papi/v1/account"
PORTFOLIO_BALANCE_PATH = "/papi/v1/balance"
FUTURES_TIME_PATH = "/fapi/v1/time"
PORTFOLIO_TIME_PATH = "/papi/v1/time"
FUTURES_INCOME_PATH = "/fapi/v1/income"
PORTFOLIO_UM_INCOME_PATH = "/papi/v1/um/income"
FUTURES_OPEN_ORDERS_PATH = "/fapi/v1/openOrders"
PORTFOLIO_UM_OPEN_ORDERS_PATH = "/papi/v1/um/openOrders"
FUTURES_OPEN_ALGO_ORDERS_PATH = "/fapi/v1/openAlgoOrders"
PORTFOLIO_UM_OPEN_CONDITIONAL_ORDERS_PATH = "/papi/v1/um/conditional/openOrders"
FUTURES_POSITION_RISK_PATH = "/fapi/v3/positionRisk"
PORTFOLIO_UM_POSITION_RISK_PATH = "/papi/v1/um/positionRisk"
INCOME_PAGE_LIMIT = 1_000
# Each income page costs 30 IP weight. Keep one interactive refresh bounded to
# 150 weight in a multi-user deployment and expose complete=False if truncated.
MAX_INCOME_PAGES = 5

Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]
AccountType = Literal["UM_FUTURE", "PORTFOLIO_MARGIN"]


class BinanceAccountClientError(RuntimeError):
    """A deliberately redacted Binance failure safe to expose as a category."""

    def __init__(self, category: str, *, code: int | None = None):
        super().__init__("Binance account request failed")
        self.category = category
        self.code = code


@dataclass(frozen=True, slots=True)
class BinanceAccountSnapshot:
    account_type: AccountType
    # Neither account endpoint proves that this API key has futures TRADE permission.
    can_trade: bool | None
    wallet_balance: Decimal
    available_balance: Decimal
    unrealized_pnl: Decimal
    currency: Literal["USD"]
    updated_at: datetime
    unrealized_pnl_by_asset: tuple[tuple[str, Decimal], ...] = ()
    positions: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class BinanceIncomeRecord:
    asset: str
    income_type: str
    income: Decimal
    time_ms: int
    symbol: str | None
    transaction_id: str | None
    trade_id: str | None


@dataclass(frozen=True, slots=True)
class BinanceIncomeHistory:
    account_type: AccountType
    records: tuple[BinanceIncomeRecord, ...]
    pages_fetched: int
    complete: bool


def current_time_ms() -> int:
    return time.time_ns() // 1_000_000


def _account_positions(value: Any, fallback_ms: int) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    output: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
            continue
        amount = _decimal_first(item, ("positionAmt", "positionAmount"), required=False)
        if amount is None or amount == 0:
            continue
        side_value = str(item.get("positionSide") or "").upper()
        side = "short" if side_value == "SHORT" or amount < 0 else "long"
        entry_price = _decimal_first(item, ("entryPrice",), required=False)
        mark_price = _decimal_first(item, ("markPrice",), required=False)
        notional = _decimal_first(item, ("notional",), required=False)
        unrealized_pnl = _decimal_first(
            item,
            ("unrealizedProfit", "unRealizedProfit", "crossUnPnl"),
            required=False,
        )
        output.append(
            {
                "symbol": item["symbol"].strip().upper()[:32],
                "amt": float(abs(amount)),
                "side": side,
                "entry_price": float(entry_price) if entry_price is not None else None,
                "mark_price": float(mark_price) if mark_price is not None else None,
                "notional": float(abs(notional)) if notional is not None else None,
                "upnl": float(unrealized_pnl) if unrealized_pnl is not None else None,
                "leverage": _position_leverage(item),
                "ts": int(_positive_int(item.get("updateTime")) or fallback_ms),
            }
        )
    return tuple(output)


def _has_open_position(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for item in value:
        if not isinstance(item, dict):
            continue
        amount = _decimal_first(item, ("positionAmt", "positionAmount"), required=False)
        if amount is not None and amount != 0:
            return True
    return False


def _position_leverage(item: dict[str, Any]) -> int | None:
    configured = _positive_int(item.get("leverage"))
    if configured is not None:
        return configured
    notional = _decimal_first(item, ("notional",), required=False)
    initial_margin = _decimal_first(
        item,
        ("positionInitialMargin", "initialMargin"),
        required=False,
    )
    if notional is None or initial_margin is None or initial_margin <= 0:
        return None
    derived = int((abs(notional) / initial_margin).to_integral_value())
    return derived if derived > 0 else None


def _open_orders(value: Any, fallback_ms: int) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise BinanceAccountClientError("invalid_response")

    output: list[dict[str, Any]] = []
    for item in value:
        symbol = item.get("symbol")
        order_id = item.get("orderId")
        if (
            not isinstance(symbol, str)
            or not symbol.strip()
            or isinstance(order_id, bool)
            or not isinstance(order_id, (int, str))
            or not str(order_id).isdigit()
        ):
            raise BinanceAccountClientError("invalid_response")

        quantity = _decimal_first(item, ("origQty",), required=True)
        executed_quantity = _decimal_first(item, ("executedQty",), required=False) or Decimal(0)
        price = _decimal_first(item, ("price",), required=False) or Decimal(0)
        average_price = _decimal_first(item, ("avgPrice",), required=False) or Decimal(0)
        stop_price = _decimal_first(item, ("stopPrice", "activatePrice"), required=False)
        created_ms = _positive_int(item.get("time")) or fallback_ms
        updated_ms = _positive_int(item.get("updateTime")) or created_ms
        output.append(
            {
                # Keep IDs as strings so browser JSON consumers never lose integer precision.
                "order_id": str(order_id),
                "client_order_id": str(item.get("clientOrderId") or "")[:128],
                "symbol": symbol.strip().upper()[:32],
                "side": str(item.get("side") or "").upper()[:8],
                "position_side": str(item.get("positionSide") or "BOTH").upper()[:8],
                "type": str(item.get("type") or item.get("origType") or "").upper()[:32],
                "status": str(item.get("status") or "").upper()[:32],
                "time_in_force": str(item.get("timeInForce") or "").upper()[:16],
                "price": float(price),
                "average_price": float(average_price),
                "stop_price": float(stop_price) if stop_price is not None else None,
                "quantity": float(quantity),
                "executed_quantity": float(executed_quantity),
                "reduce_only": item.get("reduceOnly") is True,
                "close_position": item.get("closePosition") is True,
                "conditional": False,
                "created_at": created_ms,
                "updated_at": updated_ms,
            }
        )
    output.sort(
        key=lambda item: (item["updated_at"], item["symbol"], item["order_id"]), reverse=True
    )
    return tuple(output)


def _open_conditional_orders(value: Any, fallback_ms: int) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise BinanceAccountClientError("invalid_response")

    output: list[dict[str, Any]] = []
    for item in value:
        symbol = item.get("symbol")
        order_id = item.get("algoId", item.get("strategyId"))
        if (
            not isinstance(symbol, str)
            or not symbol.strip()
            or isinstance(order_id, bool)
            or not isinstance(order_id, (int, str))
            or not str(order_id).isdigit()
        ):
            raise BinanceAccountClientError("invalid_response")

        quantity = _decimal_first(item, ("quantity", "origQty"), required=True)
        price = _decimal_first(item, ("price",), required=False) or Decimal(0)
        trigger_price = _decimal_first(
            item,
            ("triggerPrice", "stopPrice", "activatePrice"),
            required=False,
        )
        created_ms = _positive_int(item.get("createTime"))
        if created_ms is None:
            created_ms = _positive_int(item.get("bookTime")) or fallback_ms
        updated_ms = _positive_int(item.get("updateTime")) or created_ms
        output.append(
            {
                "order_id": str(order_id),
                "client_order_id": str(
                    item.get("clientAlgoId") or item.get("newClientStrategyId") or ""
                )[:128],
                "symbol": symbol.strip().upper()[:32],
                "side": str(item.get("side") or "").upper()[:8],
                "position_side": str(item.get("positionSide") or "BOTH").upper()[:8],
                "type": str(item.get("orderType") or item.get("strategyType") or "").upper()[:32],
                "status": str(item.get("algoStatus") or item.get("strategyStatus") or "").upper()[
                    :32
                ],
                "time_in_force": str(item.get("timeInForce") or "").upper()[:16],
                "price": float(price),
                "average_price": 0.0,
                "stop_price": float(trigger_price) if trigger_price is not None else None,
                "quantity": float(quantity),
                "executed_quantity": 0.0,
                "reduce_only": item.get("reduceOnly") is True,
                "close_position": item.get("closePosition") is True,
                "conditional": True,
                "created_at": created_ms,
                "updated_at": updated_ms,
            }
        )
    output.sort(
        key=lambda item: (item["updated_at"], item["symbol"], item["order_id"]),
        reverse=True,
    )
    return tuple(output)


def signed_query(
    api_secret: str,
    recv_window_ms: int,
    timestamp_ms: int,
    params: Iterable[tuple[str, str | int]] = (),
) -> str:
    query_params = [(name, str(value)) for name, value in params]
    query_params.extend([("recvWindow", str(recv_window_ms)), ("timestamp", str(timestamp_ms))])
    query = urlencode(query_params)
    signature = hmac.new(
        api_secret.encode("utf-8"), query.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{query}&signature={signature}"


def _http_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    parsed = urlsplit(url)
    connection = HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise BinanceAccountClientError("invalid_response")
        return response.status, body
    finally:
        connection.close()


class BinanceAccountClient:
    """Read account totals from Portfolio Margin, then standard USD-M as fallback."""

    def __init__(
        self,
        futures_base_url: str,
        portfolio_base_url: str,
        *,
        recv_window_ms: int = 5_000,
        timeout_seconds: float = 4.0,
        transport: Transport | None = None,
    ):
        self.futures_base_url = _approved_origin(
            futures_base_url,
            {"fapi.binance.com", "demo-fapi.binance.com"},
            "Binance Futures",
        )
        self.portfolio_base_url = _approved_origin(
            portfolio_base_url,
            {"papi.binance.com"},
            "Binance Portfolio Margin",
        )
        if not 1_000 <= recv_window_ms <= 5_000:
            raise ValueError("recvWindow must be between 1000 and 5000 milliseconds")
        if not 1 <= timeout_seconds <= 10:
            raise ValueError("timeout must be between 1 and 10 seconds")
        self.recv_window_ms = recv_window_ms
        self.timeout_seconds = timeout_seconds
        self.transport = transport or _http_transport
        self._clock_offsets_ms: dict[str, int] = {}

    def account(self, api_key: str, api_secret: str) -> BinanceAccountSnapshot:
        if not _safe_credential(api_key) or not _safe_credential(api_secret):
            raise BinanceAccountClientError("credential_error")

        # Most keys are standard USD-M. Query FAPI first so a slow or unavailable
        # PAPI origin cannot block an otherwise healthy futures account.
        futures_host = urlsplit(self.futures_base_url).hostname
        futures_failure: BinanceAccountClientError | None = None
        try:
            return self._futures_account(api_key, api_secret)
        except BinanceAccountClientError as exc:
            futures_failure = exc
            if futures_host == "demo-fapi.binance.com":
                raise
            if exc.code != -2015 and exc.category not in {
                "timeout",
                "network",
            }:
                raise

        try:
            return self._portfolio_account(api_key, api_secret)
        except BinanceAccountClientError as portfolio_error:
            # If FAPI was merely unavailable and PAPI proves this is not a portfolio
            # key, retain the actionable FAPI connectivity error.
            if (
                futures_failure is not None
                and futures_failure.code != -2015
                and portfolio_error.code == -2015
            ):
                raise futures_failure from portfolio_error
            raise

    def income_history(
        self,
        api_key: str,
        api_secret: str,
        *,
        account_type: AccountType,
        start_time_ms: int,
        end_time_ms: int,
    ) -> BinanceIncomeHistory:
        """Read one bounded monthly income window without mixing account types."""

        if not _safe_credential(api_key) or not _safe_credential(api_secret):
            raise BinanceAccountClientError("credential_error")
        if (
            isinstance(start_time_ms, bool)
            or isinstance(end_time_ms, bool)
            or not isinstance(start_time_ms, int)
            or not isinstance(end_time_ms, int)
            or start_time_ms < 0
            or end_time_ms < start_time_ms
        ):
            raise ValueError("invalid Binance income time range")
        _datetime_from_ms(start_time_ms)
        _datetime_from_ms(end_time_ms)
        if account_type == "PORTFOLIO_MARGIN":
            base_url = self.portfolio_base_url
            path = PORTFOLIO_UM_INCOME_PATH
        elif account_type == "UM_FUTURE":
            base_url = self.futures_base_url
            path = FUTURES_INCOME_PATH
        else:
            raise ValueError("unsupported Binance account type")

        records: list[BinanceIncomeRecord] = []
        seen: dict[tuple[Any, ...], tuple[Any, ...]] = {}
        complete = False
        pages_fetched = 0
        for page in range(1, MAX_INCOME_PAGES + 1):
            payload, _ = self._signed_get(
                base_url,
                path,
                api_key,
                api_secret,
                params=(
                    ("startTime", start_time_ms),
                    ("endTime", end_time_ms),
                    ("page", page),
                    ("limit", INCOME_PAGE_LIMIT),
                ),
            )
            pages_fetched += 1
            if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
                raise BinanceAccountClientError("invalid_response")
            if len(payload) > INCOME_PAGE_LIMIT:
                raise BinanceAccountClientError("invalid_response")
            new_records = 0
            for item in payload:
                record = _income_record(item)
                # Binance documents both boundaries as inclusive. Enforce the same
                # boundary locally in case an upstream page changes while paginating.
                if not start_time_ms <= record.time_ms <= end_time_ms:
                    continue
                content = (
                    record.asset,
                    record.time_ms,
                    record.income,
                    record.symbol,
                    record.trade_id,
                )
                if record.transaction_id is not None:
                    # Binance documents tranId as unique per user + incomeType.
                    key: tuple[Any, ...] = (
                        "transaction",
                        record.income_type,
                        record.transaction_id,
                    )
                else:
                    key = ("content", record.income_type, *content)
                previous = seen.get(key)
                if previous is not None and previous != content:
                    # Never double-count an identity whose financial fields changed
                    # while the live history was being paginated.
                    raise BinanceAccountClientError("invalid_response")
                if previous is None:
                    seen[key] = content
                    records.append(record)
                    new_records += 1
            if len(payload) < INCOME_PAGE_LIMIT:
                complete = True
                break
            if new_records == 0:
                # Stop if a gateway ignores page instead of burning all remaining weight.
                break

        records.sort(
            key=lambda item: (
                item.time_ms,
                item.asset,
                item.income_type,
                item.transaction_id or "",
                item.trade_id or "",
            )
        )
        return BinanceIncomeHistory(
            account_type=account_type,
            records=tuple(records),
            pages_fetched=pages_fetched,
            complete=complete,
        )

    def open_orders(
        self,
        api_key: str,
        api_secret: str,
        *,
        account_type: AccountType,
    ) -> tuple[dict[str, Any], ...]:
        """Read and normalize all currently open USD-M futures orders."""

        if not _safe_credential(api_key) or not _safe_credential(api_secret):
            raise BinanceAccountClientError("credential_error")
        if account_type == "PORTFOLIO_MARGIN":
            base_url = self.portfolio_base_url
            path = PORTFOLIO_UM_OPEN_ORDERS_PATH
            conditional_path = PORTFOLIO_UM_OPEN_CONDITIONAL_ORDERS_PATH
        elif account_type == "UM_FUTURE":
            base_url = self.futures_base_url
            path = FUTURES_OPEN_ORDERS_PATH
            conditional_path = FUTURES_OPEN_ALGO_ORDERS_PATH
        else:
            raise ValueError("unsupported Binance account type")

        payload, fetched_ms = self._signed_get(base_url, path, api_key, api_secret)
        conditional_payload, conditional_fetched_ms = self._signed_get(
            base_url,
            conditional_path,
            api_key,
            api_secret,
        )
        orders = [
            *_open_orders(payload, fetched_ms),
            *_open_conditional_orders(conditional_payload, conditional_fetched_ms),
        ]
        orders.sort(
            key=lambda item: (item["updated_at"], item["symbol"], item["order_id"]),
            reverse=True,
        )
        return tuple(orders)

    def _portfolio_account(self, api_key: str, api_secret: str) -> BinanceAccountSnapshot:
        account_payload, account_fetched_ms = self._signed_get(
            self.portfolio_base_url,
            PORTFOLIO_ACCOUNT_PATH,
            api_key,
            api_secret,
        )
        if not isinstance(account_payload, dict):
            raise BinanceAccountClientError("invalid_response")

        # The account endpoint supplies portfolio-level equity/available balance;
        # /balance supplies the UM unrealized PnL and asset update timestamp.
        balance_payload, balance_fetched_ms = self._signed_get(
            self.portfolio_base_url,
            PORTFOLIO_BALANCE_PATH,
            api_key,
            api_secret,
        )
        if not isinstance(balance_payload, list) or not all(
            isinstance(item, dict) for item in balance_payload
        ):
            raise BinanceAccountClientError("invalid_response")
        balance_rows = balance_payload
        preferred_row = _preferred_balance_row(balance_rows)

        wallet_balance = _decimal_first(
            account_payload, ("accountEquity", "actualEquity"), required=False
        )
        if wallet_balance is None:
            wallet_balance = _decimal_first(
                preferred_row,
                ("totalWalletBalance", "umWalletBalance"),
                required=True,
            )
        available_balance = _decimal_first(
            account_payload,
            ("totalAvailableBalance", "virtualMaxWithdrawAmount"),
            required=False,
        )
        if available_balance is None:
            available_balance = _decimal_first(
                preferred_row, ("crossMarginFree", "umWalletBalance"), required=True
            )
        unrealized_pnl = _decimal_first(
            account_payload,
            ("totalUnrealizedProfit", "umUnrealizedPNL"),
            required=False,
        )
        if unrealized_pnl is None:
            unrealized_pnl = _decimal_first(
                preferred_row, ("umUnrealizedPNL",), required=False
            ) or Decimal(0)

        account_updated_ms = _positive_int(account_payload.get("updateTime"))
        balance_updated_ms = _max_update_time(balance_rows)
        source_times = [
            value for value in (account_updated_ms, balance_updated_ms) if value is not None
        ]
        updated_ms = max(source_times) if source_times else None
        fallback_ms = max(account_fetched_ms, balance_fetched_ms)
        positions = account_payload.get("positions")
        if _has_open_position(positions):
            positions, position_fetched_ms = self._signed_get(
                self.portfolio_base_url,
                PORTFOLIO_UM_POSITION_RISK_PATH,
                api_key,
                api_secret,
            )
            if not isinstance(positions, list) or not all(
                isinstance(item, dict) for item in positions
            ):
                raise BinanceAccountClientError("invalid_response")
            fallback_ms = max(fallback_ms, position_fetched_ms)
            position_updated_ms = _max_update_time(positions)
            if position_updated_ms is not None:
                updated_ms = max(updated_ms or 0, position_updated_ms)
        return BinanceAccountSnapshot(
            account_type="PORTFOLIO_MARGIN",
            can_trade=None,
            wallet_balance=wallet_balance,
            available_balance=available_balance,
            unrealized_pnl=unrealized_pnl,
            currency="USD",
            updated_at=_updated_at(updated_ms, fallback_ms),
            unrealized_pnl_by_asset=_unrealized_by_asset(balance_rows, ("umUnrealizedPNL",)),
            positions=_account_positions(positions, fallback_ms),
        )

    def _futures_account(self, api_key: str, api_secret: str) -> BinanceAccountSnapshot:
        payload, fetched_ms = self._signed_get(
            self.futures_base_url,
            FUTURES_ACCOUNT_PATH,
            api_key,
            api_secret,
        )
        if not isinstance(payload, dict):
            raise BinanceAccountClientError("invalid_response")
        assets = payload.get("assets")
        positions = payload.get("positions")
        if _has_open_position(positions):
            positions, position_fetched_ms = self._signed_get(
                self.futures_base_url,
                FUTURES_POSITION_RISK_PATH,
                api_key,
                api_secret,
            )
            if not isinstance(positions, list) or not all(
                isinstance(item, dict) for item in positions
            ):
                raise BinanceAccountClientError("invalid_response")
            fetched_ms = max(fetched_ms, position_fetched_ms)
        update_sources: list[dict[str, Any]] = []
        if isinstance(assets, list):
            update_sources.extend(item for item in assets if isinstance(item, dict))
        if isinstance(positions, list):
            update_sources.extend(item for item in positions if isinstance(item, dict))
        return BinanceAccountSnapshot(
            account_type="UM_FUTURE",
            # V3 deliberately removed configuration fields such as canTrade.
            can_trade=None,
            wallet_balance=_decimal_first(payload, ("totalWalletBalance",), required=True),
            available_balance=_decimal_first(payload, ("availableBalance",), required=True),
            unrealized_pnl=_decimal_first(payload, ("totalUnrealizedProfit",), required=True),
            currency="USD",
            updated_at=_updated_at(_max_update_time(update_sources), fetched_ms),
            unrealized_pnl_by_asset=_unrealized_by_asset(
                [item for item in assets if isinstance(item, dict)]
                if isinstance(assets, list)
                else [],
                ("unrealizedProfit", "crossUnPnl"),
            ),
            positions=_account_positions(positions, fetched_ms),
        )

    def _signed_get(
        self,
        base_url: str,
        path: str,
        api_key: str,
        api_secret: str,
        *,
        params: Iterable[tuple[str, str | int]] = (),
    ) -> tuple[dict[str, Any] | list[Any], int]:
        for attempt in range(2):
            fetched_ms = current_time_ms() + self._clock_offsets_ms.get(base_url, 0)
            query = signed_query(
                api_secret,
                self.recv_window_ms,
                fetched_ms,
                params,
            )
            url = f"{base_url}{path}?{query}"
            try:
                status_code, body = self.transport(
                    url,
                    {"X-MBX-APIKEY": api_key, "Accept": "application/json"},
                    self.timeout_seconds,
                )
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
            error_payload = payload if isinstance(payload, dict) else {}
            code = _error_code(error_payload)
            if status_code < 200 or status_code >= 300 or (code is not None and code < 0):
                error = BinanceAccountClientError(
                    _error_category(status_code, error_payload), code=code
                )
                if code == -1021 and attempt == 0:
                    try:
                        self._synchronize_clock(base_url)
                    except BinanceAccountClientError:
                        raise error from None
                    continue
                raise error
            if not isinstance(payload, (dict, list)):
                raise BinanceAccountClientError("invalid_response")
            return payload, fetched_ms
        raise BinanceAccountClientError("timestamp", code=-1021)

    def _synchronize_clock(self, base_url: str) -> None:
        path = (
            PORTFOLIO_TIME_PATH
            if urlsplit(base_url).hostname == "papi.binance.com"
            else FUTURES_TIME_PATH
        )
        started_ms = current_time_ms()
        try:
            status_code, body = self.transport(
                f"{base_url}{path}",
                {"Accept": "application/json"},
                self.timeout_seconds,
            )
        except BinanceAccountClientError:
            raise
        except TimeoutError:
            raise BinanceAccountClientError("timeout") from None
        except OSError:
            raise BinanceAccountClientError("network") from None
        finished_ms = current_time_ms()
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            raise BinanceAccountClientError("invalid_response") from None
        if status_code < 200 or status_code >= 300 or not isinstance(payload, dict):
            error_payload = payload if isinstance(payload, dict) else {}
            raise BinanceAccountClientError(_error_category(status_code, error_payload))
        server_time = payload.get("serverTime")
        if isinstance(server_time, bool) or not isinstance(server_time, (int, str)):
            raise BinanceAccountClientError("invalid_response")
        try:
            server_time_ms = int(server_time)
        except (TypeError, ValueError):
            raise BinanceAccountClientError("invalid_response") from None
        midpoint_ms = started_ms + (finished_ms - started_ms) // 2
        offset_ms = server_time_ms - midpoint_ms
        if server_time_ms <= 0 or abs(offset_ms) > 86_400_000:
            raise BinanceAccountClientError("invalid_response")
        self._clock_offsets_ms[base_url] = offset_ms


def _approved_origin(base_url: str, allowed_hosts: set[str], label: str) -> str:
    parsed = urlsplit(base_url)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"unapproved {label} base URL")
    return base_url.rstrip("/")


def _safe_credential(value: str) -> bool:
    return (
        bool(value)
        and value.isascii()
        and value.isprintable()
        and not any(character.isspace() for character in value)
    )


def _error_code(payload: dict[str, Any]) -> int | None:
    if "code" not in payload:
        return None
    try:
        return int(payload["code"])
    except (TypeError, ValueError):
        return None


def _error_category(status_code: int, payload: dict[str, Any]) -> str:
    code = _error_code(payload)
    if code in {-1002, -2014, -2015, -2017, -1022} or status_code == 401:
        return "authentication"
    if code == -1021:
        return "timestamp"
    if code == -1003 or status_code in {418, 429}:
        return "rate_limit"
    if status_code == 408:
        return "timeout"
    if status_code >= 500:
        return "upstream"
    return "rejected"


def _decimal_first(
    payload: dict[str, Any], names: Iterable[str], *, required: bool
) -> Decimal | None:
    for name in names:
        if name not in payload or payload[name] is None:
            continue
        raw_value = str(payload[name]).strip()
        if not raw_value:
            continue
        try:
            value = Decimal(raw_value)
        except (InvalidOperation, TypeError, ValueError):
            raise BinanceAccountClientError("invalid_response") from None
        if not value.is_finite() or not math.isfinite(float(value)):
            raise BinanceAccountClientError("invalid_response")
        return value
    if required:
        raise BinanceAccountClientError("invalid_response")
    return None


def _preferred_balance_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for preferred_asset in ("USDT", "USDC", "BUSD"):
        for row in rows:
            if str(row.get("asset", "")).upper() == preferred_asset:
                return row
    return rows[0] if rows else {}


def _unrealized_by_asset(
    rows: Iterable[dict[str, Any]], names: Iterable[str]
) -> tuple[tuple[str, Decimal], ...]:
    totals: dict[str, Decimal] = {}
    for row in rows:
        asset = _identifier(row.get("asset"), required=True, max_length=32)
        value = _decimal_first(row, names, required=False)
        if value is not None:
            totals[asset] = totals.get(asset, Decimal(0)) + value
    return tuple(sorted(totals.items()))


def _income_record(payload: dict[str, Any]) -> BinanceIncomeRecord:
    income = _decimal_first(payload, ("income",), required=True)
    if income is None:  # pragma: no cover - required=True is exhaustive
        raise BinanceAccountClientError("invalid_response")
    raw_time = payload.get("time")
    if isinstance(raw_time, bool) or not isinstance(raw_time, (int, str)):
        raise BinanceAccountClientError("invalid_response")
    if isinstance(raw_time, str) and not re.fullmatch(r"[0-9]+", raw_time.strip()):
        raise BinanceAccountClientError("invalid_response")
    try:
        time_ms = int(raw_time)
    except (TypeError, ValueError):
        raise BinanceAccountClientError("invalid_response") from None
    if time_ms < 0:
        raise BinanceAccountClientError("invalid_response")
    _datetime_from_ms(time_ms)
    return BinanceIncomeRecord(
        asset=_identifier(payload.get("asset"), required=True, max_length=32),
        income_type=_identifier(payload.get("incomeType"), required=True, max_length=64),
        income=income,
        time_ms=time_ms,
        symbol=_identifier(payload.get("symbol"), required=False, max_length=64),
        transaction_id=_optional_scalar(payload.get("tranId")),
        trade_id=_optional_scalar(payload.get("tradeId")),
    )


def _identifier(value: Any, *, required: bool, max_length: int) -> str | None:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if required:
            raise BinanceAccountClientError("invalid_response")
        return None
    if not isinstance(value, str):
        raise BinanceAccountClientError("invalid_response")
    normalized = value.strip().upper()
    if len(normalized) > max_length or not re.fullmatch(r"[A-Z0-9_.:/-]+", normalized):
        raise BinanceAccountClientError("invalid_response")
    return normalized


def _optional_scalar(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise BinanceAccountClientError("invalid_response")
    if str(value).strip() == "":
        return None
    normalized = str(value).strip()
    if len(normalized) > 128 or not normalized.isascii() or not normalized.isprintable():
        raise BinanceAccountClientError("invalid_response")
    return normalized


def _max_update_time(rows: Iterable[dict[str, Any]]) -> int | None:
    values: list[int] = []
    for row in rows:
        value = _positive_int(row.get("updateTime"))
        if value is not None:
            values.append(value)
    return max(values) if values else None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _updated_at(value: int | None, fallback_ms: int) -> datetime:
    timestamp_ms = value if value is not None and value > 0 else fallback_ms
    return _datetime_from_ms(timestamp_ms)


def _datetime_from_ms(timestamp_ms: int) -> datetime:
    try:
        return datetime.fromtimestamp(timestamp_ms / 1_000, UTC)
    except (OSError, OverflowError, ValueError):
        raise BinanceAccountClientError("invalid_response") from None


# Compatibility name for callers created before Portfolio Margin support.
BinanceFuturesClient = BinanceAccountClient
