from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from decimal import Decimal

from quantdesk_v2.binance_client import BinanceAccountSnapshot
from quantdesk_v2.binance_service import BinanceAccountService


def _snapshot() -> BinanceAccountSnapshot:
    return BinanceAccountSnapshot(
        account_type="UM_FUTURE",
        can_trade=None,
        wallet_balance=Decimal("100"),
        available_balance=Decimal("90"),
        unrealized_pnl=Decimal("1"),
        currency="USD",
        updated_at=datetime(2026, 8, 4, tzinfo=UTC),
    )


class _Client:
    def __init__(self) -> None:
        self.account_calls = 0

    def account(self, _api_key: str, _api_secret: str) -> BinanceAccountSnapshot:
        self.account_calls += 1
        time.sleep(0.02)
        return _snapshot()


def test_concurrent_account_reads_are_coalesced() -> None:
    client = _Client()
    service = BinanceAccountService(client, account_cache_seconds=3)  # type: ignore[arg-type]
    barrier = threading.Barrier(3)
    results: list[BinanceAccountSnapshot] = []

    def read_account() -> None:
        barrier.wait()
        results.append(service.account("key", "secret"))

    threads = [threading.Thread(target=read_account) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert client.account_calls == 1
    assert len(results) == 2
    assert results[0] is results[1]


def test_account_cache_is_isolated_by_complete_credentials() -> None:
    client = _Client()
    service = BinanceAccountService(client, account_cache_seconds=3)  # type: ignore[arg-type]

    service.account("key", "first-secret")
    service.account("key", "second-secret")

    assert client.account_calls == 2


def test_force_refresh_bypasses_recent_account_cache() -> None:
    client = _Client()
    service = BinanceAccountService(client, account_cache_seconds=30)  # type: ignore[arg-type]

    service.account("key", "secret")
    service.account("key", "secret", force_refresh=True)

    assert client.account_calls == 2
