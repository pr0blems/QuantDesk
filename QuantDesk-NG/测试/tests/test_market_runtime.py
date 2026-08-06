from __future__ import annotations

import math
import struct
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantdesk import binance_client, indicators, notify, ws_ticker


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self) -> bytes:
        return self.payload


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

    result = binance_client._get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=7)

    assert result == {"ok": True}
    assert calls == [("https://fapi.binance.com/fapi/v1/exchangeInfo", 7, "GET")]


def test_binance_redirect_and_error_output_cannot_escape_or_leak_signature(
    monkeypatch,
) -> None:
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
            urllib.request.Request(
                "https://papi.binance.com/papi/v1/um/positionRisk",
                headers={"X-MBX-APIKEY": "unit-test-key"},
            ),
            None,
            302,
            "redirect",
            {},
            "https://fapi.binance.com/fapi/v1/positionRisk",
        )

    def offline(*_, **__):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(binance_client._BINANCE_OPENER, "open", offline)
    monkeypatch.setattr(binance_client.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError) as raised:
        binance_client._signed_get(
            binance_client.PAPI,
            "/papi/v1/um/positionRisk",
            "unit-test-key",
            "unit-test-secret",
        )
    message = str(raised.value)
    assert "signature=" not in message
    assert "unit-test-secret" not in message


def test_indicator_outputs_and_keyword_compatibility() -> None:
    assert indicators.sma([1, 2, 3], 2) == 2.5
    assert indicators.sma_series([1, 2, 3], 2) == [None, 1.5, 2.5]
    assert indicators.ema_series([1, 2, 3], 2) == pytest.approx([1, 5 / 3, 23 / 9])
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


def test_notification_uses_fixed_argv_and_reports_subprocess_failure(monkeypatch, caplog) -> None:
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
