import json
from datetime import date
from urllib.error import HTTPError

import pytest

from quantdesk_v2.unusual_whales import (
    UnusualWhalesMarketClient,
    UnusualWhalesMarketError,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return json.dumps(self.payload).encode()


def test_stock_state_normalizes_session_ohlcv(monkeypatch) -> None:
    payload = {
        "data": {
            "close": "182.91",
            "high": "184.00",
            "low": "180.50",
            "market_time": "premarket",
            "open": "181.00",
            "prev_close": "180.00",
            "tape_time": "2026-08-17T08:10:00Z",
            "total_volume": 12345,
            "volume": 100,
        }
    }
    monkeypatch.setattr(
        "quantdesk_v2.unusual_whales.urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )
    client = UnusualWhalesMarketClient(lambda: "server-side-test-key")

    state = client.stock_state("aapl")

    assert state["symbol"] == "AAPL"
    assert state["price"] == 182.91
    assert state["market_time"] == "premarket"


def test_market_tide_uses_latest_five_minute_sample(monkeypatch) -> None:
    payload = {
        "data": [
            {
                "timestamp": "2026-08-17T09:30:00-04:00",
                "net_call_premium": "100",
                "net_put_premium": "300",
                "net_volume": -10,
            },
            {
                "timestamp": "2026-08-17T09:35:00-04:00",
                "net_call_premium": "900",
                "net_put_premium": "200",
                "net_volume": 40,
            },
        ]
    }
    monkeypatch.setattr(
        "quantdesk_v2.unusual_whales.urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )
    client = UnusualWhalesMarketClient(lambda: "server-side-test-key")

    tide = client.market_tide()

    assert tide["bias"] == "bull"
    assert tide["net_premium"] == 700
    assert tide["samples"] == 2


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (["price", {"channel": "market_tide"}, {"name": "flow-alerts"}],
         ["price", "market_tide", "flow-alerts"]),
        ({"channels": [{"topic": "gex"}, "net_flow"]}, ["gex", "net_flow"]),
        ({"price": True, "news": False}, ["price"]),
    ],
)
def test_websocket_channels_accepts_bounded_contract_envelopes(
    monkeypatch,
    data,
    expected,
) -> None:
    monkeypatch.setattr(
        "quantdesk_v2.unusual_whales.urlopen",
        lambda *_args, **_kwargs: _Response({"data": data}),
    )

    contract = UnusualWhalesMarketClient(lambda: "key").websocket_channels()

    assert contract["channels"] == expected
    assert contract["quality"]["available"] is True


def test_stock_quote_normalizes_nbbo_and_quality(monkeypatch) -> None:
    payload = {
        "data": {
            "last_trade": {"price": "190.10", "time": 1786636349747, "vol": 12345},
            "market_time": "regular",
            "quote": {
                "bid": {"price": "190.00", "size": 200},
                "ask": {"price": "190.20", "size": 100},
                "time": 1786636351248,
            },
            "quote_values": {
                "midpoint": "190.10",
                "spread_bps": "10.52",
                "quote_age_ms": 88,
                "size_imbalance": 0.3333,
            },
        }
    }
    monkeypatch.setattr(
        "quantdesk_v2.unusual_whales.urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )

    quote = UnusualWhalesMarketClient(lambda: "key").stock_quote("aapl")

    assert quote["bid_size"] == 200
    assert quote["ask_size"] == 100
    assert quote["midpoint"] == 190.10
    assert quote["quality"]["available"] is True
    assert "missing_nbbo" not in quote["quality"]["issues"]


def test_gex_and_off_lit_endpoints_validate_and_aggregate(monkeypatch) -> None:
    requested: list[str] = []

    def fake_open(request, **_kwargs):
        requested.append(request.full_url)
        if "gex-levels" in request.full_url:
            return _Response(
                {
                    "data": {
                        "call_wall": "210",
                        "put_wall": "180",
                        "gamma_flip": "194.5",
                        "gamma_magnet": "200",
                    }
                }
            )
        return _Response(
            {
                "data": [
                    {"price": 190, "lit_vol": 100, "off_vol": 300},
                    {"price": 191, "lit_vol": 50, "off_vol": 50},
                    {"price": None, "lit_vol": 999, "off_vol": 999},
                ]
            }
        )

    monkeypatch.setattr("quantdesk_v2.unusual_whales.urlopen", fake_open)
    client = UnusualWhalesMarketClient(lambda: "key")

    gex = client.gex_levels("AAPL", market_date=date(2026, 8, 14))
    levels = client.off_lit_price_levels("AAPL", market_date="2026-08-14")

    assert gex["gamma_flip"] == 194.5
    assert "date=2026-08-14" in requested[0]
    assert levels["levels"][0]["price"] == 190
    assert levels["total_off_lit_volume"] == 350
    assert levels["off_lit_ratio"] == pytest.approx(350 / 500)
    with pytest.raises(UnusualWhalesMarketError):
        client.gex_levels("AAPL", market_date="2026-02-31")


def test_calendar_and_vix_term_structure_are_bounded(monkeypatch) -> None:
    requested: list[str] = []

    def fake_open(request, **_kwargs):
        requested.append(request.full_url)
        if "economic-calendar" in request.full_url:
            return _Response(
                {
                    "data": [
                        {
                            "event": "FOMC rate decision",
                            "time": "2026-08-19T18:00:00Z",
                            "type": "fomc",
                            "forecast": "4.25%",
                            "prev": "4.50%",
                        },
                        {"event": "missing time"},
                    ]
                }
            )
        return _Response(
            {
                "data": {
                    "latest": {"date": "2026-08-14", "front_month": "18.4"},
                    "history": [{"date": "2026-08-13", "front_month": "17.9"}],
                }
            }
        )

    monkeypatch.setattr("quantdesk_v2.unusual_whales.urlopen", fake_open)
    client = UnusualWhalesMarketClient(lambda: "key")

    calendar = client.economic_calendar()
    term = client.vix_term_structure(history_days=120)

    assert [item["event"] for item in calendar["events"]] == ["FOMC rate decision"]
    assert term["latest"]["front_month"] == "18.4"
    assert "history_days=120" in requested[-1]
    with pytest.raises(UnusualWhalesMarketError):
        client.vix_term_structure(history_days=366)


def test_retryable_http_errors_are_classified_and_bounded(monkeypatch) -> None:
    attempts = 0
    waits: list[float] = []

    def fail(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise HTTPError("https://api.unusualwhales.com", 429, "limited", None, None)

    monkeypatch.setattr("quantdesk_v2.unusual_whales.urlopen", fail)
    client = UnusualWhalesMarketClient(
        lambda: "key",
        max_retries=1,
        retry_backoff_seconds=0.2,
        wait=waits.append,
    )

    with pytest.raises(UnusualWhalesMarketError) as captured:
        client.market_tide()

    assert attempts == 2
    assert waits == [0.2]
    assert captured.value.category == "rate_limit"
    assert captured.value.status_code == 429
    assert captured.value.retryable is True
