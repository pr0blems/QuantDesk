"""WebSocket 全市场行情推送（1 秒级）：纯 stdlib 实现

- 订阅 fstream !miniTicker@arr：每秒推送全部合约的最新价/24h开盘/最高最低/成交额
- 支持系统 HTTP 代理（CONNECT 隧道），无代理直连
- 断线指数退避重连；REST 轮询作为兜底同时运行
"""
import base64, json, os, socket, ssl, struct, time, urllib.request

WS_HOST = "fstream.binance.com"
WS_PATH = "/ws/!miniTicker@arr"


def _connect():
    proxies = urllib.request.getproxies()
    proxy = proxies.get("https") or proxies.get("http")
    if proxy:
        from urllib.parse import urlparse
        pu = urlparse(proxy if "://" in proxy else "http://" + proxy)
        sock = socket.create_connection((pu.hostname, pu.port or 8080), timeout=15)
        sock.sendall(f"CONNECT {WS_HOST}:443 HTTP/1.1\r\nHost: {WS_HOST}:443\r\n\r\n".encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("代理 CONNECT 中断")
            resp += chunk
        line = resp.split(b"\r\n")[0]
        if b" 200" not in line:
            raise RuntimeError("代理拒绝 CONNECT: " + line.decode(errors="ignore"))
    else:
        sock = socket.create_connection((WS_HOST, 443), timeout=15)
    ctx = ssl.create_default_context()
    ssock = ctx.wrap_socket(sock, server_hostname=WS_HOST)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {WS_PATH} HTTP/1.1\r\nHost: {WS_HOST}\r\nUpgrade: websocket\r\n"
           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    ssock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = ssock.recv(4096)
        if not chunk:
            raise RuntimeError("WS 握手中断")
        resp += chunk
    line = resp.split(b"\r\n")[0]
    if b"101" not in line:
        raise RuntimeError("WS 握手被拒: " + line.decode(errors="ignore"))
    return ssock


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接断开")
        buf += chunk
    return buf


def _read_frame(sock):
    hdr = _recv_exact(sock, 2)
    fin = hdr[0] & 0x80
    op = hdr[0] & 0x0F
    ln = hdr[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    payload = _recv_exact(sock, ln) if ln else b""
    return fin, op, payload


def _pong(sock):
    mask = os.urandom(4)
    sock.sendall(b"\x8a\x80" + mask)  # FIN+pong，掩码空负载


def ws_loop(on_rows):
    """on_rows: 回调 [(symbol, price, pct_24h, quote_volume, ts)]"""
    backoff = 5
    while True:
        try:
            sock = _connect()
            sock.settimeout(90)  # 推送流持续有数据；90s 无帧判死
            print("[ws] 行情推送已连接（1秒级实时流）")
            backoff = 5
            frag = b""
            while True:
                fin, op, payload = _read_frame(sock)
                if op == 0x9:      # ping → pong
                    _pong(sock)
                    continue
                if op == 0x8:
                    raise ConnectionError("服务端主动关闭")
                if op not in (0x1, 0x0):
                    continue
                frag += payload
                if not fin:
                    continue
                data, frag = json.loads(frag), b""
                now = int(time.time())
                rows = []
                for t in data:
                    try:
                        c, o = float(t["c"]), float(t["o"])
                        pct = (c - o) / o * 100 if o else 0.0
                        rows.append((t["s"], c, pct, float(t.get("q", 0)), now))
                    except Exception:
                        continue
                on_rows(rows)
        except Exception as e:
            print(f"[ws] 推送断开（{str(e)[:60]}），{backoff}s 后重连（REST 兜底运行中）")
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)
