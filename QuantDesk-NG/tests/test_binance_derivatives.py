from __future__ import annotations

import json

from quantdesk import binance_client, realtime


def test_public_derivative_endpoints_validate_and_normalize(monkeypatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **_: object):
        calls.append(url)
        if "/basis?" in url:
            return [{"timestamp": 123, "basis": "0.01", "annualizedBasisRate": "0.3"}]
        if "adlQuantile" in url:
            return [{"symbol": "BTCUSDT", "adlQuantile": [1, 2, 3, 4, 5]}]
        return [{"symbol": "BTCUSDT", "orderId": 9}]

    monkeypatch.setattr(binance_client, "_get", fake_get)
    assert binance_client.fetch_basis("btcusdt")[0]["basis"] == "0.01"
    assert binance_client.fetch_adl_risk("btcusdt")[0]["symbol"] == "BTCUSDT"
    assert binance_client.fetch_force_orders("btcusdt")[0]["orderId"] == 9
    assert all(url.startswith(binance_client.FAPI) for url in calls)


def test_force_order_stream_is_buffered_and_drained() -> None:
    state = realtime._RealtimeState()
    state.ingest(
        {
            "e": "forceOrder",
            "E": 1234,
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "o": "LIMIT",
                "f": "IOC",
                "p": "100",
                "ap": "99.5",
                "q": "2",
                "i": 123,
                "T": 1200,
            },
        }
    )
    rows = state.drain_liquidations()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["notional"] == 199.0
    assert state.drain_liquidations() == []


def test_persist_liquidations_uses_idempotent_key(monkeypatch) -> None:
    calls: list[tuple[str, list[tuple]]] = []

    monkeypatch.setattr(
        realtime.store,
        "executemany",
        lambda query, values: calls.append((query, values)),
    )
    count = realtime._persist_liquidations(
        [
            {
                "event_id": "55",
                "symbol": "BTCUSDT",
                "event_time": 1000,
                "trade_time": 1000,
                "side": "SELL",
                "price": 100,
                "average_price": 99,
                "quantity": 2,
                "notional": 198,
                "payload": {"x": 1},
            }
        ]
    )
    assert count == 1
    assert calls
    assert calls[0][1][0][0] == "BTCUSDT:55"
    assert json.loads(calls[0][1][0][-1]) == {"x": 1}
