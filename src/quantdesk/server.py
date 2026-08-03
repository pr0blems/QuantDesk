"""本地 HTTP 服务：静态前端 + JSON API（纯 stdlib）"""
import json, os, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from . import store
from .config_loader import settings, tradfi_symbols, symbols_meta
from .paths import WEB_DIR
from . import engine

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}

def _watchlist():
    return set(store.kv_get("watchlist", settings.get("watchlist", [])))

def api_overview():
    tickers = {r["symbol"]: dict(r) for r in store.query("SELECT * FROM ticker")}
    positions = {p["symbol"]: p for p in engine.state_snapshot()["positions"]}
    watch = _watchlist()
    # 各 symbol 最新评分（三周期）
    score_rows = store.query("""
        SELECT s.symbol, s.tf, s.score FROM scores s
        JOIN (SELECT symbol, tf, MAX(open_time) mo FROM scores GROUP BY symbol, tf) m
        ON s.symbol=m.symbol AND s.tf=m.tf AND s.open_time=m.mo""")
    per = {}
    for r in score_rows:
        per.setdefault(r["symbol"], {})[r["tf"]] = r["score"]
    w = settings.get("timeframe_weights", {"15m": .3, "1h": .4, "4h": .3})
    out = []
    meta = {s["symbol"]: s for s in symbols_meta.get("symbols", [])}
    trending = set((store.kv_get("st_trending", {}) or {}).get("symbols") or [])
    for sym in tradfi_symbols():
        t = tickers.get(sym, {})
        tf_scores = per.get(sym, {})
        num = sum(tf_scores.get(tf, 0) * wt for tf, wt in w.items() if tf in tf_scores)
        den = sum(wt for tf, wt in w.items() if tf in tf_scores)
        combined = round(num / den) if den else None
        base = sym.replace("USDT", "").replace("USD1", "")
        out.append({
            "symbol": sym,
            "underlying": meta.get(sym, {}).get("underlyingType", ""),
            "price": t.get("price"), "pct_24h": t.get("pct_24h"),
            "score": combined, "tf_scores": tf_scores,
            "watch": sym in watch,
            "position": positions.get(sym),
            "trending": base in trending,
        })
    return out

def api_score_detail(symbol):
    rows = store.query("""
        SELECT s.tf, s.score, s.detail, s.open_time FROM scores s
        JOIN (SELECT tf, MAX(open_time) mo FROM scores WHERE symbol=? GROUP BY tf) m
        ON s.tf=m.tf AND s.open_time=m.mo WHERE s.symbol=?""", (symbol, symbol))
    out = {}
    for r in rows:
        out[r["tf"]] = {"score": r["score"], "open_time": r["open_time"],
                        "factors": json.loads(r["detail"] or "[]")}
    return out

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p == "/api/overview": return self._json(api_overview())
            if p == "/api/klines":
                sym = q.get("symbol", [""])[0]; tf = q.get("tf", ["1h"])[0]
                return self._json(store.get_klines(sym, tf, int(q.get("limit", ["300"])[0])))
            if p == "/api/score": return self._json(api_score_detail(q.get("symbol", [""])[0]))
            if p == "/api/report":
                from . import report as rep
                return self._json(rep.build_report(q.get("symbol", [""])[0]))
            if p == "/api/breadth":
                from . import report as rep
                return self._json(rep.market_breadth())
            if p == "/api/positions": return self._json(engine.state_snapshot()["positions"])
            if p == "/api/alerts":
                lim = int(q.get("limit", ["100"])[0])
                rows = store.query("SELECT * FROM alerts ORDER BY ts DESC LIMIT ?", (lim,))
                return self._json([dict(r) for r in rows])
            if p == "/api/news":
                lim = int(q.get("limit", ["60"])[0])
                rows = store.query("SELECT * FROM news ORDER BY ts DESC LIMIT ?", (lim,))
                return self._json([dict(r) for r in rows])
            if p == "/api/watchlist": return self._json(sorted(_watchlist()))
            if p == "/api/state": return self._json(engine.state_snapshot())
            if p == "/api/paper":
                from . import paper
                return self._json(paper.api_data())
            # 静态文件
            fp = os.path.normpath(os.path.join(WEB_DIR, p.lstrip("/") or "index.html"))
            if not fp.startswith(WEB_DIR) or not os.path.isfile(fp):
                fp = os.path.join(WEB_DIR, "index.html")
            ext = os.path.splitext(fp)[1]
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln) or b"{}")
            if u.path == "/api/watchlist":
                store.kv_set("watchlist", body.get("symbols", []))
                return self._json({"ok": True})
            if u.path == "/api/alerts/read":
                store.execute("UPDATE alerts SET read=1")
                return self._json({"ok": True})
            if u.path == "/api/paper/reset":
                from . import paper
                paper.reset()
                return self._json({"ok": True})
            return self._json({"error": "unknown"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

def start_server(port=None):
    port = port or settings.get("http_port", 8100)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True, name="http").start()
    print(f"[http] 工作台已启动: http://127.0.0.1:{port}")
    return srv
