from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass

from .binance_client import (
    AccountType,
    BinanceAccountClient,
    BinanceAccountSnapshot,
    BinanceIncomeHistory,
)


@dataclass(frozen=True, slots=True)
class _CachedAccount:
    stored_at: float
    snapshot: BinanceAccountSnapshot


@dataclass(frozen=True, slots=True)
class _CachedOpenOrders:
    stored_at: float
    orders: tuple[dict, ...]


class BinanceAccountService:
    """Share Binance clock state and coalesce concurrent account snapshots."""

    def __init__(
        self,
        client: BinanceAccountClient,
        *,
        account_cache_seconds: float = 3.0,
        open_orders_cache_seconds: float = 30.0,
    ) -> None:
        if account_cache_seconds < 0 or open_orders_cache_seconds < 0:
            raise ValueError("Binance cache durations must not be negative")
        self.client = client
        self.account_cache_seconds = account_cache_seconds
        self.open_orders_cache_seconds = open_orders_cache_seconds
        self._guard = threading.Lock()
        self._account_locks: dict[str, threading.Lock] = {}
        self._account_cache: dict[str, _CachedAccount] = {}
        self._open_orders_cache: dict[tuple[str, AccountType], _CachedOpenOrders] = {}

    @staticmethod
    def _credential_id(api_key: str, api_secret: str) -> str:
        digest = hashlib.sha256()
        digest.update(api_key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(api_secret.encode("utf-8"))
        return digest.hexdigest()

    def account(
        self, api_key: str, api_secret: str, *, force_refresh: bool = False
    ) -> BinanceAccountSnapshot:
        credential_id = self._credential_id(api_key, api_secret)
        with self._guard:
            account_lock = self._account_locks.setdefault(
                credential_id, threading.Lock()
            )

        # FastAPI executes these synchronous routes in worker threads. Serialize only
        # requests for the same credentials so a page load does not call /account
        # two or three times in parallel.
        with account_lock:
            now = time.monotonic()
            with self._guard:
                cached = self._account_cache.get(credential_id)
            if (
                not force_refresh
                and cached is not None
                and now - cached.stored_at <= self.account_cache_seconds
            ):
                return cached.snapshot

            snapshot = self.client.account(api_key, api_secret)
            with self._guard:
                self._account_cache[credential_id] = _CachedAccount(
                    stored_at=time.monotonic(),
                    snapshot=snapshot,
                )
            return snapshot

    def open_orders(
        self,
        api_key: str,
        api_secret: str,
        *,
        account_type: AccountType,
        force_refresh: bool = False,
    ) -> tuple[dict, ...]:
        credential_id = self._credential_id(api_key, api_secret)
        cache_key = (credential_id, account_type)
        with self._guard:
            account_lock = self._account_locks.setdefault(
                credential_id, threading.Lock()
            )

        # The all-symbol normal and conditional order endpoints are expensive.
        # Share one short-lived result between the dashboard and reconciliation
        # instead of issuing a fresh pair of weight-40 requests per browser poll.
        with account_lock:
            now = time.monotonic()
            with self._guard:
                cached = self._open_orders_cache.get(cache_key)
            if (
                not force_refresh
                and cached is not None
                and now - cached.stored_at <= self.open_orders_cache_seconds
            ):
                return cached.orders

            orders = self.client.open_orders(
                api_key,
                api_secret,
                account_type=account_type,
            )
            with self._guard:
                self._open_orders_cache[cache_key] = _CachedOpenOrders(
                    stored_at=time.monotonic(),
                    orders=orders,
                )
            return orders

    def income_history(
        self,
        api_key: str,
        api_secret: str,
        *,
        account_type: AccountType,
        start_time_ms: int,
        end_time_ms: int,
    ) -> BinanceIncomeHistory:
        return self.client.income_history(
            api_key,
            api_secret,
            account_type=account_type,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
        )

    def invalidate(self, api_key: str, api_secret: str) -> None:
        credential_id = self._credential_id(api_key, api_secret)
        with self._guard:
            self._account_cache.pop(credential_id, None)
            for key in [key for key in self._open_orders_cache if key[0] == credential_id]:
                self._open_orders_cache.pop(key, None)
