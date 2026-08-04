from __future__ import annotations

import json
import math
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantdesk_v2 import binance_rate_limit as rate_limit_module
from quantdesk_v2 import indicators, market_engine, notify, ws_ticker
from quantdesk_v2 import market_data_client as binance_client
from quantdesk_v2.binance_client import BinanceAccountClient, BinanceAccountClientError
from quantdesk_v2.binance_rate_limit import (
    REST_RATE_LIMITER,
    BinanceRestRateLimit,
    BinanceRestRateLimiter,
    rest_request_weight,
)
from quantdesk_v2.binance_trading import BinanceUsdMTradingClient


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return self.payload

    def close(self) -> None:
        return None


class _Socket:
    def __init__(self, incoming: bytes = b""):
        self.incoming = bytearray(incoming)
        self.sent = bytearray()

    def recv(self, length: int) -> bytes:
        chunk = bytes(self.incoming[:length])
        del self.incoming[:length]
        return chunk

    def sendall(self, payload: bytes) -> None:
        self.sent.extend(payload)


@pytest.mark.parametrize(
    "url",
    [
        "http://fapi.binance.com/fapi/v1/ticker/24hr",
        "https://fapi.binance.com.attacker.invalid/fapi/v1/ticker/24hr",
        "https://user@fapi.binance.com/fapi/v1/ticker/24hr",
        "https://fapi.binance.com:444/fapi/v1/ticker/24hr",
        "https://fapi.binance.com/fapi/v1/\r\nInjected: value",
        "https://fapi.binance.com/fapi/v1/\tbad",
        "file:///etc/passwd",
    ],
)
def test_binance_http_client_rejects_non_allowlisted_urls(url: str, monkeypatch) -> None:
    def forbidden_open(*_, **__):
        raise AssertionError("network must not run for a rejected URL")

    monkeypatch.setattr(binance_client._BINANCE_OPENER, "open", forbidden_open)
    with pytest.raises(ValueError, match="Binance URL"):
        binance_client._get(url)


def test_binance_http_client_accepts_exact_https_origin(monkeypatch) -> None:
    calls = []

    def open_request(request, timeout):
        calls.append((request.full_url, timeout, request.get_method()))
        return _Response(b'{"ok": true}')

    monkeypatch.setattr(binance_client._BINANCE_OPENER, "open", open_request)

    result = binance_client._get(
        "https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=7
    )

    assert result == {"ok": True}
    assert calls == [
        ("https://fapi.binance.com/fapi/v1/exchangeInfo", 7, "GET")
    ]


def test_binance_rest_limiter_opens_one_shared_circuit_from_418() -> None:
    limiter = BinanceRestRateLimiter()
    deadline_ms = int((time.time() + 180) * 1_000)

    limiter.observe(
        418,
        {},
        json.dumps(
            {
                "code": -1003,
                "msg": f"Way too many requests; IP banned until {deadline_ms}.",
            }
        ),
    )

    snapshot = limiter.snapshot()
    assert snapshot.blocked is True
    assert snapshot.retry_after_seconds > 170
    with pytest.raises(BinanceRestRateLimit):
        limiter.before_request(1)


def test_binance_rest_limiter_honors_longest_deadline_and_expires(
    monkeypatch,
) -> None:
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr(
        rate_limit_module,
        "time",
        SimpleNamespace(time=lambda: clock["now"], sleep=lambda _: None),
    )
    limiter = BinanceRestRateLimiter()
    ban_deadline = clock["now"] + 90

    limiter.observe(
        418,
        {"Retry-After": "120"},
        json.dumps(
            {
                "code": -1003,
                "msg": f"IP banned until {int(ban_deadline * 1_000)}.",
            }
        ),
    )

    assert limiter.snapshot().retry_at == pytest.approx(clock["now"] + 120)
    with pytest.raises(BinanceRestRateLimit):
        limiter.before_request(1)
    clock["now"] += 121
    limiter.before_request(1)
    assert limiter.snapshot().blocked is False


def test_binance_rest_limiter_uses_weight_headers_and_restores_deadline(
    monkeypatch,
) -> None:
    clock = {"now": 1_800_000_030.0}
    monkeypatch.setattr(
        rate_limit_module,
        "time",
        SimpleNamespace(time=lambda: clock["now"], sleep=lambda _: None),
    )
    limiter = BinanceRestRateLimiter()
    limiter.observe(
        200,
        {"X-MBX-USED-WEIGHT-1M": str(limiter.safe_weight_per_minute)},
        b"{}",
    )
    expected_boundary = (int(clock["now"]) // 60 + 1) * 60 + 1
    assert limiter.snapshot().retry_at == expected_boundary

    restored = BinanceRestRateLimiter()
    assert restored.restore_blocked_until(expected_boundary) is True
    assert restored.snapshot().retry_at == expected_boundary
    restored_from_ms = BinanceRestRateLimiter()
    assert restored_from_ms.restore_blocked_until(expected_boundary * 1_000) is True
    assert restored_from_ms.snapshot().retry_at == expected_boundary
    assert restored.restore_blocked_until(float("inf")) is False


def test_binance_rest_weight_estimates_cover_heavy_all_symbol_routes() -> None:
    assert rest_request_weight("GET", "https://fapi.binance.com/fapi/v1/ticker/24hr") == 40
    assert (
        rest_request_weight(
            "GET",
            "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=15m&limit=300",
        )
        == 2
    )
    assert rest_request_weight("GET", "https://fapi.binance.com/fapi/v1/income") == 30


def test_public_418_is_not_retried_and_blocks_followup_requests(monkeypatch) -> None:
    calls = 0
    deadline_ms = int((time.time() + 180) * 1_000)

    def rate_limited(*_, **__):
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError(
            "https://fapi.binance.com/fapi/v1/ticker/24hr",
            418,
            "rate limited",
            {},
            _Response(
                json.dumps(
                    {
                        "code": -1003,
                        "msg": f"Way too many requests; IP banned until {deadline_ms}.",
                    }
                ).encode()
            ),
        )

    REST_RATE_LIMITER.reset()
    monkeypatch.setattr(binance_client._BINANCE_OPENER, "open", rate_limited)
    try:
        with pytest.raises(BinanceRestRateLimit):
            binance_client.fetch_tickers()
        with pytest.raises(BinanceRestRateLimit):
            binance_client.fetch_tickers()
        assert calls == 1
    finally:
        REST_RATE_LIMITER.reset()


def test_public_418_also_fences_signed_account_and_trade_clients(monkeypatch) -> None:
    deadline_ms = int((time.time() + 180) * 1_000)
    account_calls = 0
    trade_calls = 0

    def account_transport(*_):
        nonlocal account_calls
        account_calls += 1
        return (
            418,
            json.dumps(
                {
                    "code": -1003,
                    "msg": f"Way too many requests; IP banned until {deadline_ms}.",
                }
            ).encode(),
        )

    def trade_transport(*_):
        nonlocal trade_calls
        trade_calls += 1
        return 200, b'{"dualSidePosition":false}'

    account = BinanceAccountClient(
        "https://fapi.binance.com",
        "https://papi.binance.com",
        transport=account_transport,
    )
    trading = BinanceUsdMTradingClient(
        "https://fapi.binance.com",
        transport=trade_transport,
    )
    REST_RATE_LIMITER.reset()
    try:
        with pytest.raises(BinanceAccountClientError) as captured:
            account.account("api-key", "api-secret")
        assert captured.value.category == "rate_limit"
        with pytest.raises(BinanceAccountClientError) as captured:
            trading.position_mode("api-key", "api-secret")
        assert captured.value.category == "rate_limit"
        assert account_calls == 1
        assert trade_calls == 0
    finally:
        REST_RATE_LIMITER.reset()


def test_kline_batch_skips_symbols_already_current(monkeypatch) -> None:
    target = 1_800_000
    monkeypatch.setattr(market_engine.store, "latest_closed_time", lambda *_: target)
    monkeypatch.setattr(
        market_engine.bc,
        "fetch_klines",
        lambda *_: (_ for _ in ()).throw(AssertionError("network must not run")),
    )
    market_engine._state["kline_cursor"] = {}

    result = market_engine._run_kline_batch(["BTCUSDT"], "15m", target, 300, 20)

    assert result["completed"] is True
    assert result["ok"] == 1


def test_kline_batch_resumes_same_symbol_after_rate_limit(monkeypatch) -> None:
    target = 1_800_000
    calls: list[str] = []
    attempts = {"ETHUSDT": 0}

    monkeypatch.setattr(market_engine.store, "latest_closed_time", lambda *_: 0)
    monkeypatch.setattr(market_engine.store, "upsert_klines", lambda *_: None)
    monkeypatch.setattr(
        market_engine,
        "time",
        SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=lambda _: None),
    )

    def fetch(symbol, *_):
        calls.append(symbol)
        if symbol == "ETHUSDT":
            attempts[symbol] += 1
            if attempts[symbol] == 1:
                raise BinanceRestRateLimit(time.time() + 180, status=418)
        return [(target, 1.0, 2.0, 0.5, 1.5, 10.0)]

    monkeypatch.setattr(market_engine.bc, "fetch_klines", fetch)
    market_engine._state["kline_cursor"] = {}
    market_engine._state["last_kline_batch"] = {}

    first = market_engine._run_kline_batch(
        ["BTCUSDT", "ETHUSDT", "SOLUSDT"], "15m", target, 300, 20
    )
    second = market_engine._run_kline_batch(
        ["BTCUSDT", "ETHUSDT", "SOLUSDT"], "15m", target, 300, 20
    )

    assert first["rate_limited"] is True
    assert first["next_index"] == 1
    assert second["completed"] is True
    assert calls == ["BTCUSDT", "ETHUSDT", "ETHUSDT", "SOLUSDT"]


def test_kline_batch_does_not_skip_transient_failure(monkeypatch) -> None:
    target = 1_800_000
    calls = 0
    monkeypatch.setattr(market_engine.store, "latest_closed_time", lambda *_: 0)
    monkeypatch.setattr(market_engine.store, "upsert_klines", lambda *_: None)
    monkeypatch.setattr(
        market_engine,
        "time",
        SimpleNamespace(time=time.time, monotonic=time.monotonic, sleep=lambda _: None),
    )

    def fetch(*_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise binance_client.BinancePublicRequestError(
                "temporary upstream failure", retryable=True, status=503
            )
        return [(target, 1.0, 2.0, 0.5, 1.5, 10.0)]

    monkeypatch.setattr(market_engine.bc, "fetch_klines", fetch)
    market_engine._state["kline_cursor"] = {}
    market_engine._state["last_kline_batch"] = {}

    first = market_engine._run_kline_batch(["BTCUSDT"], "15m", target, 300, 20)
    second = market_engine._run_kline_batch(["BTCUSDT"], "15m", target, 300, 20)

    assert first["retryable_error"] is True
    assert first["next_index"] == 0
    assert second["completed"] is True
    assert calls == 2


def test_market_engine_restores_persisted_rest_circuit(monkeypatch) -> None:
    deadline = time.time() + 180
    writes = []
    REST_RATE_LIMITER.reset()
    monkeypatch.setattr(
        market_engine.store,
        "system_state_get",
        lambda key, default=0: deadline,
    )
    monkeypatch.setattr(
        market_engine.store,
        "system_state_set",
        lambda key, value: writes.append((key, value)),
    )
    market_engine._state["rest_blocked_until_persisted"] = 0
    try:
        assert market_engine._restore_public_rest_circuit() is True
        assert REST_RATE_LIMITER.snapshot().retry_at == pytest.approx(deadline)
        assert writes == []
    finally:
        REST_RATE_LIMITER.reset()
        market_engine._state["rest_blocked_until_persisted"] = 0


def test_ticker_ingestion_is_serialized_between_ws_and_rest(monkeypatch) -> None:
    active = 0
    maximum_active = 0
    guard = threading.Lock()
    start = threading.Barrier(3)

    monkeypatch.setattr(market_engine, "tradfi_symbols", lambda: ["BTCUSDT"])
    monkeypatch.setattr(market_engine.store, "admin_alert_rules", lambda: {})
    market_engine._state["price_hist"] = {}

    def write(*_):
        nonlocal active, maximum_active
        with guard:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.03)
        with guard:
            active -= 1

    monkeypatch.setattr(market_engine.store, "executemany", write)

    def ingest():
        start.wait()
        market_engine.ingest_tickers(
            [("BTCUSDT", 100.0, 1.0, 1_000.0, int(time.time()))]
        )

    workers = [threading.Thread(target=ingest) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait()
    for worker in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert maximum_active == 1


def test_market_engine_starts_one_ws_and_one_rest_fallback(monkeypatch) -> None:
    started = []

    class _Thread:
        def __init__(self, *, target, args=(), daemon=None, name=None):
            self.target = target
            self.args = args
            self.daemon = daemon
            self.name = name

        def start(self):
            started.append(self)

    monkeypatch.setattr(market_engine.threading, "Thread", _Thread)
    monkeypatch.setattr(market_engine, "_restore_public_rest_circuit", lambda: False)
    monkeypatch.setitem(market_engine.settings, "paper_trading", False)
    market_engine._state["started"] = False
    try:
        market_engine.start()
        market_engine.start()
    finally:
        market_engine._state["started"] = False

    names = [thread.name for thread in started]
    assert names.count("ticker-ws") == 1
    assert names.count("ticker-rest-fallback") == 1
    assert "price" not in names
    assert "ticker" not in names


def test_rest_fallback_does_not_poll_while_ws_is_fresh(monkeypatch) -> None:
    class _StopLoop(RuntimeError):
        pass

    monkeypatch.setattr(
        market_engine,
        "time",
        SimpleNamespace(
            time=lambda: 1_000,
            monotonic=lambda: 1_000,
            sleep=lambda _: (_ for _ in ()).throw(_StopLoop()),
        ),
    )
    monkeypatch.setattr(market_engine.store, "collector_paused", lambda *_: False)
    monkeypatch.setattr(
        market_engine.bc,
        "fetch_tickers",
        lambda: (_ for _ in ()).throw(AssertionError("REST must remain idle")),
    )
    market_engine._state["started_at"] = 900
    market_engine._state["last_ws_ticker"] = 995

    with pytest.raises(_StopLoop):
        market_engine.ticker_loop()


def test_binance_kline_range_is_paginated_and_only_returns_closed_bars(
    monkeypatch,
) -> None:
    interval_ms = 15 * 60_000
    start_ms = 1_700_000_100_000
    aligned_start = start_ms - start_ms % interval_ms
    rows = [
        [aligned_start + index * interval_ms, "10", "12", "9", "11", "100"]
        for index in range(3)
    ]
    calls: list[dict[str, list[str]]] = []

    def get_page(url: str, **_):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        calls.append(query)
        cursor = int(query["startTime"][0])
        limit = int(query["limit"][0])
        return [row for row in rows if row[0] >= cursor][:limit]

    monkeypatch.setattr(binance_client, "_get", get_page)
    monkeypatch.setattr(binance_client, "_BINANCE_KLINE_PAGE_SIZE", 2)
    monkeypatch.setattr(
        binance_client.time,
        "time",
        lambda: (rows[-1][0] + interval_ms * 2) / 1_000,
    )

    result = binance_client.fetch_klines_range(
        "AAPLUSDT",
        "15m",
        start_ms,
        rows[-1][0],
    )

    assert [row[0] for row in result] == [row[0] for row in rows]
    assert len(calls) == 2
    assert calls[1]["startTime"] == [str(rows[1][0] + interval_ms)]


def test_binance_kline_range_rejects_unbounded_or_unsupported_requests() -> None:
    with pytest.raises(ValueError, match="symbol"):
        binance_client.fetch_klines_range("../AAPL", "15m", 0, 1)
    with pytest.raises(ValueError, match="interval"):
        binance_client.fetch_klines_range("AAPLUSDT", "5m", 0, 1)
    with pytest.raises(ValueError, match="bar limit"):
        binance_client.fetch_klines_range(
            "AAPLUSDT",
            "15m",
            0,
            10 * 15 * 60_000,
            max_bars=2,
        )


def test_binance_redirect_cannot_escape_the_public_market_origin() -> None:
    handler = binance_client._BinanceRedirectHandler()
    with pytest.raises(ValueError, match="approved HTTPS origin"):
        handler.redirect_request(
            None,
            None,
            302,
            "redirect",
            {},
            "https://attacker.invalid/collect",
        )
    with pytest.raises(ValueError, match="original HTTPS origin"):
        handler.redirect_request(
            urllib.request.Request("https://papi.binance.com/papi/v1/time"),
            None,
            302,
            "redirect",
            {},
            "https://fapi.binance.com/fapi/v1/positionRisk",
        )


def test_indicator_outputs_and_keyword_compatibility() -> None:
    assert indicators.sma([1, 2, 3], 2) == 2.5
    assert indicators.sma_series([1, 2, 3], 2) == [None, 1.5, 2.5]
    assert indicators.ema_series([1, 2, 3], 2) == pytest.approx(
        [1, 5 / 3, 23 / 9]
    )
    assert indicators.rsi([1, 2, 3], n=2) == 100
    assert indicators.atr([10, 12, 13], [8, 9, 11], [9, 11, 12], n=2) == 2.5

    lower, middle, upper = indicators.bollinger([1, 2, 3], n=3, mult=1)
    assert middle == 2
    assert lower == pytest.approx(2 - math.sqrt(2 / 3))
    assert upper == pytest.approx(2 + math.sqrt(2 / 3))
    assert indicators.obv([1, 2, 1, 1], [10, 20, 30, 40]) == [0, 20, -10, -10]

    closes = [float(value) for value in range(1, 21)]
    assert all(value is not None for value in indicators.macd(closes, 3, 5, 3))
    assert (
        indicators.supertrend(
            [value + 1 for value in closes],
            [value - 1 for value in closes],
            closes,
            n=3,
            mult=2,
        )
        == 1
    )


def test_notification_uses_fixed_argv_and_reports_subprocess_failure(
    monkeypatch, caplog
) -> None:
    fixed_shell = notify._POWERSHELL_CANDIDATES[0]
    calls = []

    monkeypatch.setattr(notify.sys, "platform", "win32")
    monkeypatch.setattr(notify, "_find_shell", lambda: fixed_shell)
    monkeypatch.setattr(notify, "_toast_system_enabled", lambda: True)
    monkeypatch.setattr(Path, "is_file", lambda _: True)

    def completed(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(notify.subprocess, "run", completed)
    notify.windows_toast("title; Write-Host bad", "body $(Get-Item Env:PATH)")

    command, options = calls[0]
    assert command[0] == str(fixed_shell)
    assert command[6] == str(notify._TOAST_PS1)
    assert command[-4:] == [
        "-Title",
        "title; Write-Host bad",
        "-Body",
        "body $(Get-Item Env:PATH)",
    ]
    assert options["shell"] is False
    assert options["check"] is False

    def timed_out(*_, **__):
        raise subprocess.TimeoutExpired("powershell", 25)

    monkeypatch.setattr(notify.subprocess, "run", timed_out)
    notify.windows_toast("title", "body")
    assert "desktop notification failed" in caplog.text


def test_websocket_proxy_validation_and_ticker_row_errors_are_explicit(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        ws_ticker.urllib.request,
        "getproxies",
        lambda: {"https": "https://proxy.invalid:8443"},
    )
    with pytest.raises(ValueError, match="plain HTTP CONNECT"):
        ws_ticker._proxy_address()

    rows, invalid = ws_ticker._ticker_rows(
        [
            {"s": "BTCUSDT", "c": "105", "o": "100", "q": "12.5"},
            {"s": "BROKEN", "c": "not-a-number", "o": "100"},
            None,
        ],
        123,
    )
    assert rows == [("BTCUSDT", 105.0, 5.0, 12.5, 123)]
    assert invalid == 2


def test_websocket_consumer_failure_does_not_require_reconnect(capsys) -> None:
    delivered = ws_ticker._deliver_rows(
        lambda _: (_ for _ in ()).throw(RuntimeError("database deadlock")),
        [("BTCUSDT", 105.0, 5.0, 12.5, 123)],
    )

    assert delivered is False
    assert "keeping stream open" in capsys.readouterr().out


def test_websocket_frames_enforce_server_masking_and_client_pong_mask() -> None:
    with pytest.raises(ConnectionError, match="must not be masked"):
        ws_ticker._read_frame(_Socket(b"\x81\x80"))

    oversized = b"\x82\x7f" + struct.pack(">Q", ws_ticker._MAX_MESSAGE_BYTES + 1)
    with pytest.raises(ConnectionError, match="safety limit"):
        ws_ticker._read_frame(_Socket(oversized))

    sock = _Socket()
    ws_ticker._pong(sock, b"ok")
    frame = bytes(sock.sent)
    assert frame[:2] == b"\x8a\x82"
    mask = frame[2:6]
    payload = bytes(value ^ mask[index % 4] for index, value in enumerate(frame[6:]))
    assert payload == b"ok"
