"""Minimal stdlib WebSocket client for Binance's all-market mini ticker stream."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

WS_HOST = "fstream.binance.com"
WS_PATH = "/ws/!miniTicker@arr"
_WS_ALLOWED_HOSTS = frozenset({"fstream.binance.com"})
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HEADER_BYTES = 64 * 1024
_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


def _proxy_address() -> tuple[str, int] | None:
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("http")
    if not proxy:
        return None
    parsed = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("system proxy contains an invalid port") from exc
    if (
        parsed.scheme.lower() != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("system proxy must be a plain HTTP CONNECT endpoint")
    return parsed.hostname, port or 8080


def _managed_proxy() -> tuple[bool, tuple[str, str, int, str | None, str | None] | None]:
    """Resolve an administrator-selected collector proxy without blocking direct fallback.

    Importing the web application here is intentionally lazy: the legacy market
    module can still be used in offline tooling, while worker processes gain the
    encrypted proxy selection stored by the V2 control plane.
    """

    try:
        from quantdesk_v2.proxy_management import collector_proxy_state

        configured, proxy = collector_proxy_state()
        if proxy is None:
            return configured, None
        return configured, (proxy.protocol, proxy.host, proxy.port, proxy.username, proxy.password)
    except Exception:
        return True, None


def _read_http_headers(sock: socket.socket) -> bytes:
    response = bytearray()
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4_096)
        if not chunk:
            raise ConnectionError("HTTP handshake ended before headers completed")
        response.extend(chunk)
        if len(response) > _MAX_HEADER_BYTES:
            raise ConnectionError("HTTP handshake headers exceed the safety limit")
    return bytes(response)


def _status_code(response: bytes) -> int:
    status_line = response.split(b"\r\n", maxsplit=1)[0]
    parts = status_line.split()
    if len(parts) < 2 or not parts[1].isdigit():
        raise ConnectionError("HTTP handshake returned an invalid status line")
    return int(parts[1])


def _response_headers(response: bytes) -> dict[str, str]:
    output = {}
    for line in response.split(b"\r\n")[1:]:
        if not line:
            break
        name, separator, value = line.partition(b":")
        if not separator:
            raise ConnectionError("WebSocket handshake returned a malformed header")
        output[name.decode("ascii", errors="strict").lower()] = value.decode(
            "ascii", errors="strict"
        ).strip()
    return output


def _connect() -> ssl.SSLSocket:
    if WS_HOST not in _WS_ALLOWED_HOSTS or not WS_PATH.startswith("/"):
        raise RuntimeError("WebSocket target is outside the Binance allowlist")

    proxy_configured, managed_proxy = _managed_proxy()
    system_proxy = _proxy_address() if managed_proxy is None and not proxy_configured else None
    proxy_target = (
        (managed_proxy[1], managed_proxy[2]) if managed_proxy else system_proxy
    )
    raw_socket = socket.create_connection(proxy_target or (WS_HOST, 443), timeout=15)
    active_socket: socket.socket = raw_socket
    try:
        if managed_proxy and managed_proxy[0] == "socks5":
            _socks5_connect(active_socket, WS_HOST, 443, managed_proxy[3], managed_proxy[4])
        elif proxy_target:
            authorization = None
            if managed_proxy and managed_proxy[3] and managed_proxy[4]:
                authorization = base64.b64encode(
                    f"{managed_proxy[3]}:{managed_proxy[4]}".encode("ascii")
                ).decode("ascii")
            connect_request = (
                f"CONNECT {WS_HOST}:443 HTTP/1.1\r\n"
                f"Host: {WS_HOST}:443\r\n"
                "Proxy-Connection: Keep-Alive\r\n"
                + (f"Proxy-Authorization: Basic {authorization}\r\n" if authorization else "")
                + "\r\n"
            )
            active_socket.sendall(connect_request.encode("ascii"))
            proxy_response = _read_http_headers(active_socket)
            if _status_code(proxy_response) != 200:
                raise ConnectionError("proxy rejected the Binance CONNECT tunnel")

        context = ssl.create_default_context()
        secure_socket = context.wrap_socket(active_socket, server_hostname=WS_HOST)
        active_socket = secure_socket
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {WS_PATH} HTTP/1.1\r\n"
            f"Host: {WS_HOST}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        secure_socket.sendall(request.encode("ascii"))
        response = _read_http_headers(secure_socket)
        if _status_code(response) != 101:
            raise ConnectionError("Binance rejected the WebSocket handshake")
        headers = _response_headers(response)
        expected_accept = base64.b64encode(
            hashlib.sha1(  # noqa: S324 - SHA-1 is mandated by RFC 6455, not used for security
                f"{key}{_WEBSOCKET_GUID}".encode("ascii"),
                usedforsecurity=False,
            ).digest()
        ).decode("ascii")
        if (
            headers.get("upgrade", "").lower() != "websocket"
            or "upgrade" not in headers.get("connection", "").lower()
            or headers.get("sec-websocket-accept") != expected_accept
        ):
            raise ConnectionError("Binance WebSocket handshake validation failed")
        return secure_socket
    except BaseException:
        active_socket.close()
        raise


def _socks5_connect(
    sock: socket.socket, host: str, port: int, username: str | None, password: str | None
) -> None:
    if username and password:
        sock.sendall(b"\x05\x01\x02")
    else:
        sock.sendall(b"\x05\x01\x00")
    method_response = _recv_exact(sock, 2)
    if method_response[0] != 5 or method_response[1] == 255:
        raise ConnectionError("SOCKS5 authentication method unavailable")
    if method_response[1] == 2:
        if not username or not password:
            raise ConnectionError("SOCKS5 proxy requires credentials")
        user_bytes, password_bytes = username.encode("ascii"), password.encode("ascii")
        if len(user_bytes) > 255 or len(password_bytes) > 255:
            raise ConnectionError("SOCKS5 credentials are too long")
        sock.sendall(
            bytes((1, len(user_bytes)))
            + user_bytes
            + bytes((len(password_bytes),))
            + password_bytes
        )
        if _recv_exact(sock, 2) != b"\x01\x00":
            raise ConnectionError("SOCKS5 authentication failed")
    elif method_response[1] != 0:
        raise ConnectionError("SOCKS5 selected unsupported authentication")
    host_bytes = host.encode("ascii")
    sock.sendall(
        b"\x05\x01\x00\x03" + bytes((len(host_bytes),)) + host_bytes + port.to_bytes(2, "big")
    )
    header = _recv_exact(sock, 4)
    if header[0] != 5 or header[1] != 0:
        raise ConnectionError("SOCKS5 proxy rejected Binance CONNECT tunnel")
    address_size = {1: 4, 4: 16}.get(header[3])
    if header[3] == 3:
        address_size = _recv_exact(sock, 1)[0]
    if address_size is None:
        raise ConnectionError("SOCKS5 proxy returned invalid address type")
    _recv_exact(sock, address_size + 2)


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < length:
        chunk = sock.recv(length - len(buffer))
        if not chunk:
            raise ConnectionError("WebSocket connection closed")
        buffer.extend(chunk)
    return bytes(buffer)


def _read_frame(sock: socket.socket) -> tuple[bool, int, bytes]:
    header = _recv_exact(sock, 2)
    final = bool(header[0] & 0x80)
    if header[0] & 0x70:
        raise ConnectionError("unsupported WebSocket extension bits")
    opcode = header[0] & 0x0F
    if header[1] & 0x80:
        raise ConnectionError("server WebSocket frames must not be masked")
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    if length > _MAX_MESSAGE_BYTES:
        raise ConnectionError("WebSocket frame exceeds the safety limit")
    if opcode >= 0x8 and (not final or length > 125):
        raise ConnectionError("invalid WebSocket control frame")
    payload = _recv_exact(sock, length) if length else b""
    return final, opcode, payload


def _pong(sock: socket.socket, payload: bytes = b"") -> None:
    if len(payload) > 125:
        raise ValueError("pong payload exceeds the WebSocket control-frame limit")
    mask = os.urandom(4)
    masked_payload = bytes(value ^ mask[index % len(mask)] for index, value in enumerate(payload))
    sock.sendall(bytes((0x8A, 0x80 | len(payload))) + mask + masked_payload)


def _ticker_rows(data: Any, timestamp: int) -> tuple[list[tuple], int]:
    if not isinstance(data, list):
        raise ValueError("Binance mini-ticker payload must be a list")
    rows = []
    invalid_rows = 0
    for item in data:
        try:
            if not isinstance(item, dict):
                raise TypeError("ticker row must be an object")
            close = float(item["c"])
            open_price = float(item["o"])
            percent = (close - open_price) / open_price * 100 if open_price else 0.0
            rows.append(
                (
                    item["s"],
                    close,
                    percent,
                    float(item.get("q", 0)),
                    timestamp,
                )
            )
        except (KeyError, TypeError, ValueError):
            invalid_rows += 1
    return rows, invalid_rows


def ws_loop(on_rows: Callable[[list[tuple]], None]) -> None:
    """Continuously pass ``(symbol, price, pct_24h, quote_volume, ts)`` rows to a callback."""

    backoff = 5
    while True:
        sock: ssl.SSLSocket | None = None
        try:
            sock = _connect()
            sock.settimeout(90)
            print("[ws] Binance mini-ticker stream connected")
            backoff = 5
            fragments = bytearray()
            while True:
                final, opcode, payload = _read_frame(sock)
                if opcode == 0x9:
                    _pong(sock, payload)
                    continue
                if opcode == 0xA:
                    continue
                if opcode == 0x8:
                    raise ConnectionError("Binance closed the WebSocket stream")
                if opcode not in (0x1, 0x0):
                    continue
                fragments.extend(payload)
                if len(fragments) > _MAX_MESSAGE_BYTES:
                    raise ConnectionError("WebSocket message exceeds the safety limit")
                if not final:
                    continue
                data = json.loads(bytes(fragments))
                fragments.clear()
                rows, invalid_rows = _ticker_rows(data, int(time.time()))
                if invalid_rows:
                    print(f"[ws] ignored {invalid_rows} invalid mini-ticker rows")
                on_rows(rows)
        except Exception as exc:
            print(
                f"[ws] stream disconnected ({str(exc)[:80]}); retrying in {backoff}s "
                "while REST fallback remains active"
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
        finally:
            if sock is not None:
                sock.close()
