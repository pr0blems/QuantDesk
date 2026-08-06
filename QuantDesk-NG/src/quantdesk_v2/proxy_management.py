"""Safe server-side proxy parsing, selection, and connectivity checks.

Only HTTP CONNECT and SOCKS5 endpoints are executable.  Clash configurations
often contain other protocols; those are reported as skipped rather than being
silently treated as a different transport.  Raw subscriptions are deliberately
processed in memory and never written to the database or audit log.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import SessionLocal
from .models import ProxyNode, ProxyRuntimeSetting, ProxySubscription, utcnow
from .security import CredentialCipher, SecurityError, api_key_fingerprint

_TARGET_HOST = "fstream.binance.com"
_TARGET_PORT = 443
_MAX_SUBSCRIPTION_BYTES = 2_000_000
_SAFE_NODE_NAME = 160


@dataclass(frozen=True, slots=True)
class ParsedProxyNode:
    name: str
    protocol: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True, slots=True)
class CollectorProxy:
    protocol: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None


def _clean_error(exc: Exception | str) -> str:
    value = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return value[:240] or "proxy connectivity check failed"


def _valid_host(raw: Any) -> str:
    host = str(raw or "").strip().lower().rstrip(".")
    if not host or len(host) > 253 or any(ord(char) < 33 or ord(char) == 127 for char in host):
        raise ValueError("proxy host is invalid")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if len(labels) < 2 or any(
            not label
            or len(label) > 63
            or not label.replace("-", "").isalnum()
            or label.startswith("-")
            or label.endswith("-")
            for label in labels
        ):
            raise ValueError("proxy host is invalid") from None
    return host


def _node(
    *, name: Any, protocol: Any, host: Any, port: Any, username: Any = None, password: Any = None
) -> ParsedProxyNode:
    normalized_protocol = str(protocol or "").strip().lower()
    if normalized_protocol == "socks":
        normalized_protocol = "socks5"
    if normalized_protocol not in {"http", "socks5"}:
        raise ValueError("unsupported proxy protocol")
    try:
        normalized_port = int(port)
    except (TypeError, ValueError):
        raise ValueError("proxy port is invalid") from None
    if not 1 <= normalized_port <= 65535:
        raise ValueError("proxy port is invalid")
    normalized_name = str(name or "").strip()[:_SAFE_NODE_NAME] or f"{host}:{normalized_port}"
    raw_username = str(username).strip() if username is not None else None
    raw_password = str(password) if password is not None else None
    if raw_username and (
        not raw_username.isascii() or any(char.isspace() for char in raw_username)
    ):
        raise ValueError("proxy username must be printable ASCII without spaces")
    if raw_password and (
        not raw_password.isascii() or any(char.isspace() for char in raw_password)
    ):
        raise ValueError("proxy password must be printable ASCII without spaces")
    return ParsedProxyNode(
        name=normalized_name,
        protocol=normalized_protocol,
        host=_valid_host(host),
        port=normalized_port,
        username=raw_username or None,
        password=raw_password or None,
    )


def sanitize_subscription_endpoint(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(
            "subscription endpoint must be an HTTP(S) URL without embedded credentials"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("subscription endpoint port is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("subscription endpoint port is invalid")
    netloc = parsed.hostname.lower()
    if port is not None:
        netloc = f"{netloc}:{port}"
    # Query strings are commonly bearer-style subscription credentials.  We do
    # not fetch endpoints in this process and therefore retain no query at all.
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))


def _maybe_base64_decode(content: str) -> str:
    compact = "".join(content.split())
    if not compact or len(compact) % 4 == 1:
        return content
    try:
        decoded = base64.urlsafe_b64decode(compact + "=" * (-len(compact) % 4))
        text = decoded.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return content
    return (
        text if ("://" in text or "proxies:" in text or text.lstrip().startswith("{")) else content
    )


def parse_subscription(content: str) -> tuple[list[ParsedProxyNode], int]:
    """Return executable nodes and a count of safely ignored unsupported entries."""

    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content.encode("utf-8")) > _MAX_SUBSCRIPTION_BYTES
    ):
        raise ValueError("subscription content is empty or too large")
    decoded = _maybe_base64_decode(content).lstrip("\ufeff").strip()
    nodes: list[ParsedProxyNode] = []
    skipped = 0
    if "proxies:" in decoded or decoded.startswith("{"):
        try:
            loaded = yaml.safe_load(decoded)
        except yaml.YAMLError as exc:
            raise ValueError("subscription YAML is invalid") from exc
        raw_nodes = loaded.get("proxies", []) if isinstance(loaded, dict) else []
        if not isinstance(raw_nodes, list):
            raise ValueError("subscription proxies must be a list")
        for raw in raw_nodes:
            if not isinstance(raw, dict):
                skipped += 1
                continue
            try:
                nodes.append(
                    _node(
                        name=raw.get("name"),
                        protocol=raw.get("type"),
                        host=raw.get("server"),
                        port=raw.get("port"),
                        username=raw.get("username"),
                        password=raw.get("password"),
                    )
                )
            except ValueError:
                skipped += 1
    else:
        for line in decoded.splitlines():
            candidate = line.strip()
            if not candidate or candidate.startswith("#"):
                continue
            try:
                parsed = urlsplit(candidate)
                nodes.append(
                    _node(
                        name=unquote(parsed.hostname or ""),
                        protocol=parsed.scheme,
                        host=parsed.hostname,
                        port=parsed.port,
                        username=unquote(parsed.username) if parsed.username else None,
                        password=unquote(parsed.password) if parsed.password else None,
                    )
                )
            except (ValueError, TypeError):
                skipped += 1
    if not nodes:
        raise ValueError("no supported HTTP or SOCKS5 proxy nodes were found")
    return nodes, skipped


def _cipher(master_key: str) -> CredentialCipher:
    return CredentialCipher(master_key)


def _upsert_node(
    db: Session, subscription_id: int | None, parsed: ParsedProxyNode, cipher: CredentialCipher
) -> ProxyNode:
    node = db.scalar(
        select(ProxyNode).where(
            ProxyNode.subscription_id == subscription_id,
            ProxyNode.protocol == parsed.protocol,
            ProxyNode.host == parsed.host,
            ProxyNode.port == parsed.port,
        )
    )
    encrypted = cipher.encrypt(parsed.password) if parsed.password else None
    fingerprint = (
        api_key_fingerprint(f"{parsed.username or ''}:{parsed.password or ''}")
        if parsed.password
        else None
    )
    if node is None:
        node = ProxyNode(
            subscription_id=subscription_id,
            name=parsed.name,
            protocol=parsed.protocol,
            host=parsed.host,
            port=parsed.port,
            username=parsed.username,
            password_encrypted=encrypted,
            credential_fingerprint=fingerprint,
        )
        db.add(node)
    else:
        node.name = parsed.name
        node.username = parsed.username
        node.password_encrypted = encrypted
        node.credential_fingerprint = fingerprint
        node.enabled = True
    return node


def import_subscription(
    db: Session,
    *,
    name: str,
    content: str,
    endpoint: str | None,
    authorization: str | None,
    enabled: bool,
    refresh_interval_minutes: int,
    master_key: str,
) -> tuple[ProxySubscription, int, int]:
    parsed_nodes, skipped = parse_subscription(content)
    cipher = _cipher(master_key)
    subscription = ProxySubscription(
        name=name.strip(),
        endpoint=sanitize_subscription_endpoint(endpoint),
        auth_encrypted=cipher.encrypt(authorization) if authorization else None,
        auth_fingerprint=api_key_fingerprint(authorization) if authorization else None,
        source_format="yaml" if "proxies:" in _maybe_base64_decode(content) else "base64",
        enabled=enabled,
        refresh_interval_minutes=refresh_interval_minutes,
        last_imported_at=utcnow(),
    )
    db.add(subscription)
    db.flush()
    for parsed in parsed_nodes:
        _upsert_node(db, subscription.id, parsed, cipher)
    return subscription, len(parsed_nodes), skipped


def add_manual_node(
    db: Session, *, parsed: ParsedProxyNode, master_key: str, enabled: bool
) -> ProxyNode:
    node = _upsert_node(db, None, parsed, _cipher(master_key))
    node.enabled = enabled
    return node


def _recv_http_headers(sock: socket.socket) -> bytes:
    response = bytearray()
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("proxy closed CONNECT response")
        response.extend(chunk)
        if len(response) > 16_384:
            raise ConnectionError("proxy CONNECT response is too large")
    return bytes(response)


def _test_http_proxy(sock: socket.socket, proxy: CollectorProxy) -> None:
    headers = [
        f"CONNECT {_TARGET_HOST}:{_TARGET_PORT} HTTP/1.1",
        f"Host: {_TARGET_HOST}:{_TARGET_PORT}",
        "Proxy-Connection: close",
    ]
    if proxy.username and proxy.password:
        token = base64.b64encode(f"{proxy.username}:{proxy.password}".encode("ascii")).decode(
            "ascii"
        )
        headers.append(f"Proxy-Authorization: Basic {token}")
    sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
    status_line = _recv_http_headers(sock).split(b"\r\n", 1)[0].split()
    if len(status_line) < 2 or status_line[1] != b"200":
        raise ConnectionError("HTTP proxy rejected Binance CONNECT")


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = sock.recv(size - len(buffer))
        if not chunk:
            raise ConnectionError("SOCKS5 proxy closed connection")
        buffer.extend(chunk)
    return bytes(buffer)


def _test_socks5_proxy(sock: socket.socket, proxy: CollectorProxy) -> None:
    if proxy.username and proxy.password:
        sock.sendall(b"\x05\x01\x02")
    else:
        sock.sendall(b"\x05\x01\x00")
    version, method = _recv_exact(sock, 2)
    if version != 5 or method == 255:
        raise ConnectionError("SOCKS5 authentication method unavailable")
    if method == 2:
        if not proxy.username or not proxy.password:
            raise ConnectionError("SOCKS5 proxy requires credentials")
        username = proxy.username.encode("ascii")
        password = proxy.password.encode("ascii")
        if len(username) > 255 or len(password) > 255:
            raise ConnectionError("SOCKS5 credentials are too long")
        sock.sendall(bytes((1, len(username))) + username + bytes((len(password),)) + password)
        if _recv_exact(sock, 2) != b"\x01\x00":
            raise ConnectionError("SOCKS5 authentication failed")
    elif method != 0:
        raise ConnectionError("SOCKS5 selected unsupported authentication")
    host = _TARGET_HOST.encode("ascii")
    sock.sendall(b"\x05\x01\x00\x03" + bytes((len(host),)) + host + _TARGET_PORT.to_bytes(2, "big"))
    header = _recv_exact(sock, 4)
    if header[0] != 5 or header[1] != 0:
        raise ConnectionError("SOCKS5 proxy rejected Binance CONNECT")
    address_type = header[3]
    address_size = {1: 4, 4: 16}.get(address_type)
    if address_type == 3:
        address_size = _recv_exact(sock, 1)[0]
    if address_size is None:
        raise ConnectionError("SOCKS5 reply address type is invalid")
    _recv_exact(sock, address_size + 2)


def test_proxy(node: ProxyNode, cipher: CredentialCipher, timeout_seconds: float = 5.0) -> int:
    password = cipher.decrypt(node.password_encrypted) if node.password_encrypted else None
    proxy = CollectorProxy(node.protocol, node.host, node.port, node.username, password)
    started = time.perf_counter()
    with socket.create_connection((proxy.host, proxy.port), timeout=timeout_seconds) as sock:
        sock.settimeout(timeout_seconds)
        if proxy.protocol == "http":
            _test_http_proxy(sock, proxy)
        else:
            _test_socks5_proxy(sock, proxy)
    return max(1, round((time.perf_counter() - started) * 1000))


def apply_test_result(
    db: Session, node: ProxyNode, master_key: str, timeout_seconds: float = 5.0
) -> ProxyNode:
    try:
        node.last_latency_ms = test_proxy(node, _cipher(master_key), timeout_seconds)
        node.health_status = "healthy"
        node.last_error = None
    except (OSError, ConnectionError, SecurityError) as exc:
        node.health_status = "unhealthy"
        node.last_latency_ms = None
        node.last_error = _clean_error(exc)
    node.last_tested_at = utcnow()
    return node


def select_best_node(db: Session) -> ProxyNode | None:
    return db.scalar(
        select(ProxyNode)
        .where(ProxyNode.enabled.is_(True), ProxyNode.health_status == "healthy")
        .order_by(ProxyNode.last_latency_ms.asc(), ProxyNode.id.asc())
        .limit(1)
    )


def runtime_setting(db: Session) -> ProxyRuntimeSetting:
    setting = db.get(ProxyRuntimeSetting, 1)
    if setting is None:
        setting = ProxyRuntimeSetting(id=1)
        db.add(setting)
        db.flush()
    return setting


def set_runtime(
    db: Session, *, enabled: bool, selection_mode: str, active_node_id: int | None
) -> ProxyRuntimeSetting:
    setting = runtime_setting(db)
    selected: ProxyNode | None = None
    if enabled and selection_mode == "manual":
        selected = db.get(ProxyNode, active_node_id)
        if selected is None or not selected.enabled:
            raise ValueError("selected proxy node is unavailable")
    elif enabled and selection_mode == "auto":
        selected = select_best_node(db)
    setting.enabled = enabled
    setting.selection_mode = selection_mode
    setting.active_node_id = selected.id if selected else None
    if not enabled or selection_mode == "direct":
        setting.fallback_state, setting.fallback_reason = "direct", "proxy disabled"
    elif selected is None:
        setting.fallback_state, setting.fallback_reason = "direct_fallback", "no healthy proxy node"
    else:
        setting.fallback_state, setting.fallback_reason = "proxy_active", None
    return setting


def collector_proxy_state() -> tuple[bool, CollectorProxy | None]:
    """Resolve a safe proxy for a collector process; failure always means direct fallback."""

    try:
        with SessionLocal() as db:
            setting = db.get(ProxyRuntimeSetting, 1)
            if setting is None or not setting.enabled or setting.selection_mode == "direct":
                # Once the managed setting exists, explicit "disabled" means
                # direct routing (never silently fall back to a system proxy).
                return setting is not None, None
            node = db.get(ProxyNode, setting.active_node_id) if setting.active_node_id else None
            if setting.selection_mode == "auto":
                node = select_best_node(db)
                setting.active_node_id = node.id if node else None
            if node is None or not node.enabled or node.health_status != "healthy":
                setting.fallback_state, setting.fallback_reason = (
                    "direct_fallback",
                    "selected node is not healthy",
                )
                db.commit()
                return True, None
            try:
                password = (
                    _cipher(get_settings().credential_master_key.get_secret_value()).decrypt(
                        node.password_encrypted
                    )
                    if node.password_encrypted
                    else None
                )
            except SecurityError:
                node.health_status = "unhealthy"
                node.last_error = "stored proxy credential could not be decrypted"
                setting.fallback_state, setting.fallback_reason = "direct_fallback", node.last_error
                db.commit()
                return True, None
            setting.fallback_state, setting.fallback_reason = "proxy_active", None
            db.commit()
            return True, CollectorProxy(
                node.protocol, node.host, node.port, node.username, password
            )
    except Exception:  # Database availability must never stop the collector direct fallback.
        return True, None


def active_collector_proxy() -> CollectorProxy | None:
    """Compatibility helper for code that only needs a usable selected proxy."""

    return collector_proxy_state()[1]
