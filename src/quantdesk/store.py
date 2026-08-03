"""SQLite 存储层：K线 / 实时快照 / 持仓 / 信号 / 舆情 / 社交情绪"""
import sqlite3, json, os, threading, time
from .paths import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "quantdesk.db")

_lock = threading.Lock()
_conn = None

def get_conn():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        init_schema(_conn)
    return _conn

def init_schema(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS klines(
        symbol TEXT, tf TEXT, open_time INTEGER,
        open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY(symbol, tf, open_time));
    CREATE TABLE IF NOT EXISTS ticker(
        symbol TEXT PRIMARY KEY, price REAL, pct_24h REAL,
        quote_volume REAL, ts INTEGER);
    CREATE TABLE IF NOT EXISTS positions(
        symbol TEXT PRIMARY KEY, amt REAL, side TEXT, entry_price REAL,
        mark_price REAL, upnl REAL, leverage INTEGER, ts INTEGER);
    CREATE TABLE IF NOT EXISTS scores(
        symbol TEXT, tf TEXT, open_time INTEGER, score REAL,
        detail TEXT, PRIMARY KEY(symbol, tf, open_time));
    CREATE TABLE IF NOT EXISTS alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, symbol TEXT, kind TEXT, direction TEXT,
        score REAL, message TEXT, detail TEXT, read INTEGER DEFAULT 0);
    CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts DESC);
    CREATE TABLE IF NOT EXISTS news(
        id TEXT PRIMARY KEY, ts INTEGER, source TEXT, lang TEXT,
        title TEXT, title_zh TEXT, link TEXT, sentiment TEXT, summary TEXT);
    CREATE INDEX IF NOT EXISTS idx_news_ts ON news(ts DESC);
    CREATE TABLE IF NOT EXISTS social(
        symbol TEXT PRIMARY KEY, st_bull INTEGER, st_bear INTEGER, st_msgs INTEGER,
        ape_mentions INTEGER, ape_upvotes INTEGER, ape_rank INTEGER, ape_rank_24h INTEGER, ts INTEGER);
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
    """)

def execute(sql, params=()):
    with _lock:
        c = get_conn()
        cur = c.execute(sql, params)
        c.commit()
        return cur

def executemany(sql, seq):
    with _lock:
        c = get_conn()
        c.executemany(sql, seq)
        c.commit()

def query(sql, params=()):
    with _lock:
        return get_conn().execute(sql, params).fetchall()

def kv_get(k, default=None):
    rows = query("SELECT v FROM kv WHERE k=?", (k,))
    return json.loads(rows[0]["v"]) if rows else default

def kv_set(k, v):
    execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))

def upsert_klines(symbol, tf, rows):
    """rows: list of (open_time, o, h, l, c, v)"""
    executemany(
        "INSERT OR REPLACE INTO klines(symbol,tf,open_time,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?,?)",
        [(symbol, tf, r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows])

def get_klines(symbol, tf, limit=300):
    rows = query(
        "SELECT open_time,open,high,low,close,volume FROM klines WHERE symbol=? AND tf=? ORDER BY open_time DESC LIMIT ?",
        (symbol, tf, limit))
    return [dict(r) for r in reversed(rows)]

def latest_closed_time(symbol, tf):
    rows = query("SELECT MAX(open_time) AS m FROM klines WHERE symbol=? AND tf=?", (symbol, tf))
    return rows[0]["m"] if rows and rows[0]["m"] else 0

def add_alert(symbol, kind, direction, score, message, detail=None):
    execute("INSERT INTO alerts(ts,symbol,kind,direction,score,message,detail) VALUES(?,?,?,?,?,?,?)",
            (int(time.time()), symbol, kind, direction, score, message,
             json.dumps(detail, ensure_ascii=False) if detail else None))
