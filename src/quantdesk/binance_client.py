"""币安数据层：公开行情轮询 + 统一账户持仓同步（纯 stdlib，带重试/限频）"""
import hmac, hashlib, json, time, urllib.request, urllib.parse, urllib.error

FAPI = "https://fapi.binance.com"
PAPI = "https://papi.binance.com"

UA = {"User-Agent": "Mozilla/5.0 (quantdesk-local)"}

def _get(url, headers=None, timeout=20, retries=3, backoff=2.0):
    hdr = dict(UA); hdr.update(headers or {})
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            last = f"HTTP {e.code}: {body}"
            if e.code in (418, 429):  # 限频：退避
                time.sleep(backoff * (i + 2))
            else:
                break
        except Exception as e:
            last = str(e)
            time.sleep(backoff * (i + 1))
    raise RuntimeError(f"GET {url} 失败: {last}")

def fetch_exchange_info():
    return _get(f"{FAPI}/fapi/v1/exchangeInfo", timeout=30)

def fetch_tickers():
    """全部合约 24h ticker（单次请求，weight 40）"""
    return _get(f"{FAPI}/fapi/v1/ticker/24hr", timeout=20)

def fetch_klines(symbol, interval, limit=300):
    q = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    data = _get(f"{FAPI}/fapi/v1/klines?{q}", timeout=20)
    # [open_time, o, h, l, c, v, close_time, ...]
    return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])) for k in data]

def _signed_get(base, path, api_key, secret, timeout=25):
    params = {"timestamp": int(time.time() * 1000), "recvWindow": 10000}
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{base}{path}?{qs}&signature={sig}"
    return _get(url, headers={"X-MBX-APIKEY": api_key}, timeout=timeout, retries=3)

def fetch_positions(api_key, secret):
    """统一账户 UM 持仓，返回 [{symbol, amt, side, entry, mark, upnl, leverage}]"""
    data = _signed_get(PAPI, "/papi/v1/um/positionRisk", api_key, secret)
    out = []
    for p in data:
        amt = float(p.get("positionAmt", 0))
        if amt == 0:
            continue
        out.append({
            "symbol": p["symbol"],
            "amt": amt,
            "side": p.get("positionSide") or ("LONG" if amt > 0 else "SHORT"),
            "entry": float(p.get("entryPrice", 0)),
            "mark": float(p.get("markPrice", 0)),
            "upnl": float(p.get("unRealizedProfit", 0)),
            "leverage": int(float(p.get("leverage", 0))),
        })
    return out
