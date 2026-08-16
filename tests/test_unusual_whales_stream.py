import json
import time

import pytest

from quantdesk_v2.unusual_whales_stream import (
    UnusualWhalesStreamClient,
    normalize_channel,
    normalize_stream_message,
)


class _FakeTransport:
    def __init__(self, messages, error="disconnected token=server-secret") -> None:
        self.messages = list(messages)
        self.error = error
        self.sent: list[dict] = []
        self.closed = False

    def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def recv(self, timeout=None):
        del timeout
        if self.messages:
            return self.messages.pop(0)
        raise ConnectionError(self.error)

    def close(self) -> None:
        self.closed = True


def _price_frame(*, event_id=None) -> str:
    payload = {
        "ticker": "AAPL",
        "close": "190.25",
        "time": int(time.time() * 1_000),
        "vol": 123_456,
    }
    if event_id:
        payload["id"] = event_id
    return json.dumps(["price:AAPL", payload])


def test_normalize_stream_price_and_off_lit_trade() -> None:
    now_ms = int(time.time() * 1_000)
    price = normalize_stream_message(
        json.dumps(
            [
                "price:SPY",
                {"ticker": "SPY", "close": "562.82", "time": now_ms, "vol": 6015555},
            ]
        ),
        received_at_ms=now_ms + 10,
    )
    off_lit = normalize_stream_message(
        json.dumps(
            [
                "off_lit_trades",
                {
                    "symbol": "AAPL",
                    "price": "150.25",
                    "size": 100,
                    "executed_at": "2026-08-16T14:30:00Z",
                },
            ]
        ),
        received_at_ms=now_ms,
        stale_after_ms=10**12,
    )

    assert price.event_type == "price"
    assert price.symbol == "SPY"
    assert price.values["price"] == 562.82
    assert price.quality["valid"] is True
    assert off_lit.values["notional"] == 15_025
    assert off_lit.values["venue"] == "off_lit"


def test_channel_validation_is_allowlisted() -> None:
    assert normalize_channel("gex:SPY") == "gex:SPY"
    assert normalize_channel("custom_alerts") == "custom_alerts"
    assert normalize_channel("trading_halts") == "trading_halts"
    with pytest.raises(ValueError):
        normalize_channel("price:https://evil.invalid")
    with pytest.raises(ValueError):
        normalize_channel("unknown")
    with pytest.raises(ValueError):
        normalize_channel("trading_halts:AAPL")
    with pytest.raises(ValueError):
        normalize_channel("net_flow")


def test_reconnect_restores_subscriptions_and_deduplicates() -> None:
    frame = _price_frame(event_id="same-event")
    transports = [_FakeTransport([frame]), _FakeTransport([frame])]
    created_urls: list[str] = []

    def factory(url: str):
        created_urls.append(url)
        return transports[len(created_urls) - 1]

    events = []
    client = UnusualWhalesStreamClient(
        lambda: "server-secret",
        events.append,
        channels=("price:AAPL", "trading_halts"),
        transport_factory=factory,
        reconnect_min_seconds=0,
        reconnect_max_seconds=0,
        wait=lambda _delay: False,
    )

    client.run_forever(max_reconnects=1)
    health = client.health_snapshot()

    expected_joins = [
        {"channel": "price:AAPL", "msg_type": "join"},
        {"channel": "trading_halts", "msg_type": "join"},
    ]
    assert transports[0].sent == expected_joins
    assert transports[1].sent == expected_joins
    assert len(events) == 1
    assert health["received"] == 2
    assert health["accepted"] == 1
    assert health["duplicates"] == 1
    assert health["reconnects"] == 1
    assert health["status"] == "degraded"
    assert "server-secret" not in (health["last_error"] or "")
    assert all(url.startswith("wss://api.unusualwhales.com/socket?") for url in created_urls)


def test_malformed_frames_do_not_break_the_stream_callback() -> None:
    transport = _FakeTransport(["not-json", _price_frame()])
    events = []
    client = UnusualWhalesStreamClient(
        lambda: "key",
        events.append,
        channels=("price:AAPL",),
        transport_factory=lambda _url: transport,
    )

    client.run_forever(max_reconnects=0)

    assert len(events) == 1
    assert client.health()["malformed"] == 1
