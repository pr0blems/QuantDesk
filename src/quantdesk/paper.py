"""模拟盘引擎：AI 信号自动交易（多空双向 / 加仓 / ATR 止损止盈 / 超时平仓 / 强平 / 拟真成本）

规则（与用户确认版 + 币安官方规则修正版）：
- 初始资金 10,000 USDT，20x 杠杆（与实盘对齐），逐仓模式（每仓独立保证金）
- 信号评分分档：60-75 → 10% 保证金；75-90 → 20%；90+（需舆情共振）→ 30%
- 最多 15 个并发仓位；同合约最多加仓 2 次（共 3 笔），加仓后按均价重算止损止盈与强平价
- 平仓：反向信号(|评分|>=60) / 1.5xATR 止损 / 2.5xATR 止盈 / 持仓满 12h 超时
- 强平（币安官方公式，逐仓近似）：
    多头强平价 = 均价 × (1 − 1/杠杆 + MMR)；空头强平价 = 均价 × (1 + 1/杠杆 − MMR)
    MMR 取币安 leverageBracket 真实第1档维持保证金率（股票≈2.5%，黄金0.5%），
    触发强平损失该仓全部保证金（破产保护：余额不为负）
- 资金费（币安官方机制）：每 8h 在 UTC 00/08/16 整点对跨结算点的持仓一次性结算，
    费用 = 名义价值 × 当期资金费率，多头付正费率（空头收），反之亦然
- 总保证金使用率上限 80%，不足时自动降档，最低档都不够则跳过
- 手续费：开平仓各 0.05%（taker，币安 VIP0 标准）+ 0.03% 滑点
- 注意：强平/止盈止损在真实币安以"标记价格"触发，模拟中用最新价近似
"""
import json, time, threading
from . import store, indicators as ind, binance_client as bc
from .config_loader import settings, tradfi_symbols

LEVERAGE = 20           # 与实盘对齐（实盘全部 20x）
FEE_PCT = 0.0005        # 单边手续费 0.05%（币安 U本位 taker VIP0）
SLIP_PCT = 0.0003       # 单边滑点 0.03%
TIMEOUT_SEC = 48 * 3600
MAX_POSITIONS = 15
MAX_ADDS = 2
MARGIN_CAP = 0.80
START_BALANCE = 10000.0
ENTRY_TH = 60           # 用户指定：4h评分±60直接开仓（注意：回测±60期望仅+13U/笔，低于±75）
MMR_DEFAULT = 0.025     # 维持保证金率兜底（股票合约真实第1档≈2.5%）
FUNDING_HOURS_UTC = (0, 8, 16)  # 币安资金费结算时刻

_lock = threading.Lock()
_funding_cache = {}     # symbol -> (ts, rate)
_mmr_cache = {"ts": 0, "map": {}}   # 全合约 MMR 缓存


# ---------- 表结构 ----------
def init():
    store.execute("""
    CREATE TABLE IF NOT EXISTS paper_positions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, side INTEGER, qty REAL, avg_entry REAL, margin REAL,
        leverage INTEGER, stop REAL, target REAL,
        adds INTEGER DEFAULT 0, opened_ts INTEGER, last_add_ts INTEGER,
        open_score INTEGER, basis TEXT, funding_acc REAL DEFAULT 0)""")
    # 兼容旧表：补强平价/资金费结算点/移动止损列
    cols = {r["name"] for r in store.query("PRAGMA table_info(paper_positions)")}
    if "liq_price" not in cols:
        store.execute("ALTER TABLE paper_positions ADD COLUMN liq_price REAL")
    if "funding_ts" not in cols:
        store.execute("ALTER TABLE paper_positions ADD COLUMN funding_ts INTEGER DEFAULT 0")
    if "atr_entry" not in cols:
        store.execute("ALTER TABLE paper_positions ADD COLUMN atr_entry REAL")
    if "peak_price" not in cols:
        store.execute("ALTER TABLE paper_positions ADD COLUMN peak_price REAL")
    if "tp_done" not in cols:
        store.execute("ALTER TABLE paper_positions ADD COLUMN tp_done INTEGER DEFAULT 0")
    store.execute("""
    CREATE TABLE IF NOT EXISTS paper_trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, side INTEGER, qty REAL, entry_price REAL, exit_price REAL,
        margin REAL, pnl REAL, fee REAL, funding REAL, reason TEXT,
        open_score INTEGER, opened_ts INTEGER, closed_ts INTEGER)""")
    store.execute("""
    CREATE TABLE IF NOT EXISTS paper_equity(ts INTEGER PRIMARY KEY, equity REAL, balance REAL)""")
    if store.kv_get("paper_balance") is None:
        store.kv_set("paper_balance", START_BALANCE)
        store.kv_set("paper_start_ts", int(time.time()))


def reset():
    with _lock:
        store.execute("DELETE FROM paper_positions")
        store.execute("DELETE FROM paper_trades")
        store.execute("DELETE FROM paper_equity")
        store.execute("DELETE FROM kv WHERE k LIKE 'paper_liq_cd:%'")
        store.kv_set("paper_balance", START_BALANCE)
        store.kv_set("paper_start_ts", int(time.time()))


# ---------- 账户 ----------
def balance():
    return float(store.kv_get("paper_balance", START_BALANCE))


def open_positions():
    return [dict(r) for r in store.query("SELECT * FROM paper_positions")]


def used_margin(positions=None):
    positions = positions if positions is not None else open_positions()
    return sum(p["margin"] for p in positions)


def upnl_of(p, price):
    """单仓浮动盈亏（不含费）"""
    return (price - p["avg_entry"]) * p["qty"] * p["side"]


def equity(prices=None, positions=None):
    positions = positions if positions is not None else open_positions()
    prices = prices or _prices()
    upnl = sum(upnl_of(p, prices.get(p["symbol"], p["avg_entry"])) for p in positions)
    return balance() + used_margin(positions) + upnl, upnl


def _prices():
    return {r["symbol"]: r["price"] for r in store.query("SELECT symbol, price FROM ticker")}


# ---------- 信号 ----------
def _signal_4h(symbol):
    """回测定案的入场信号源：最新 4h 评分 + 4h ATR + 主要依据"""
    rows = store.query("""
        SELECT s.score, s.detail FROM scores s
        JOIN (SELECT MAX(open_time) mo FROM scores WHERE symbol=? AND tf='4h') m
        ON s.open_time=m.mo WHERE s.symbol=? AND s.tf='4h'""", (symbol, symbol))
    if not rows:
        return None, None, []
    score = rows[0]["score"]
    atr = None
    kl = store.get_klines(symbol, "4h", 60)
    if len(kl) > 15:
        atr = ind.atr([k["high"] for k in kl], [k["low"] for k in kl], [k["close"] for k in kl])
    basis = []
    for f in json.loads(rows[0]["detail"] or "[]"):
        if f.get("weight", 0) > 0:
            basis.append((abs(f.get("contribution", 0)), f"[4h·{f['name']}] {f['reason']}"))
    basis = [b for _, b in sorted(basis, key=lambda x: -x[0])[:3]]
    return score, atr, basis


def _sentiment_aligned(symbol, side):
    """舆情共振检查：近12h 相关新闻情绪 + Stocktwits 多空比是否与方向一致。
    返回 True=共振/无数据, False=对立"""
    from .report import _match_news
    try:
        news, direct = _match_news(symbol, limit=20, hours=12)
    except Exception:
        news, direct = [], False
    bull = sum(1 for n in news if n.get("sentiment") == "bull" and not n.get("macro"))
    bear = sum(1 for n in news if n.get("sentiment") == "bear" and not n.get("macro"))
    base = symbol.replace("USDT", "").replace("USD1", "")
    srows = store.query("SELECT st_bull, st_bear FROM social WHERE symbol=?", (base,))
    st_bull_pct = None
    if srows:
        t = (srows[0]["st_bull"] or 0) + (srows[0]["st_bear"] or 0)
        if t >= 5:
            st_bull_pct = (srows[0]["st_bull"] or 0) / t * 100
    opposing = False
    if bull + bear >= 2:
        opposing = (bear > bull) if side > 0 else (bull > bear)
    if st_bull_pct is not None:
        if (side > 0 and st_bull_pct < 35) or (side < 0 and st_bull_pct > 65):
            opposing = True
    return not opposing


def _risk_pct(score, symbol):
    """评分 → 每笔风险预算（占权益比例，GitHub 主流风险定量法）：
    60-90 → 1.5%；90+ 且舆情共振 → 2.5%（仓位由止损距离反推，见 _open）"""
    a = abs(score)
    if a >= 90:
        return 0.025 if _sentiment_aligned(symbol, 1 if score > 0 else -1) else 0.015
    if a >= ENTRY_TH:
        return 0.015
    return 0.0


def _funding_rate(symbol):
    """当前资金费率（缓存 1h，失败按 0）"""
    ts_rate = _funding_cache.get(symbol)
    if ts_rate and time.time() - ts_rate[0] < 3600:
        return ts_rate[1]
    rate = 0.0
    try:
        import urllib.parse
        data = bc._get(f"{bc.FAPI}/fapi/v1/premiumIndex?{urllib.parse.urlencode({'symbol': symbol})}", timeout=10, retries=1)
        rate = float(data.get("lastFundingRate") or 0)
    except Exception:
        pass
    _funding_cache[symbol] = (time.time(), rate)
    return rate


def _load_mmr():
    """从币安 leverageBracket 拉全部合约第1档维持保证金率（6h 刷新一次）"""
    if time.time() - _mmr_cache["ts"] < 6 * 3600 and _mmr_cache["map"]:
        return
    try:
        from .config_loader import api_keys
        bk = api_keys.get("binance", {})
        data = bc._signed_get(bc.PAPI, "/papi/v1/um/leverageBracket", bk["api_key"], bk["api_secret"], timeout=30)
        m = {}
        for item in data:
            try:
                m[item["symbol"]] = float(item["brackets"][0]["maintMarginRatio"])
            except Exception:
                continue
        if m:
            _mmr_cache["map"] = m
            _mmr_cache["ts"] = time.time()
            print(f"[paper] MMR 档位已更新（{len(m)} 个合约）")
    except Exception as e:
        print(f"[paper] MMR 拉取失败，沿用缓存/默认: {str(e)[:60]}")


def _mmr(symbol):
    _load_mmr()
    return _mmr_cache["map"].get(symbol, MMR_DEFAULT)


def _liq_price(avg_entry, side, mmr):
    """币安官方强平价公式（逐仓近似）：
    多头 = 均价 × (1 − 1/杠杆 + MMR)；空头 = 均价 × (1 + 1/杠杆 − MMR)"""
    if side > 0:
        return avg_entry * (1 - 1 / LEVERAGE + mmr)
    return avg_entry * (1 + 1 / LEVERAGE - mmr)


def _exit_multipliers(atr, price):
    """B·波动自适应档位：按 ATR 占现价比决定止损/移动止损倍数
    高波动(≥1.5%) → 放宽防扫损；低波动(<0.7%) → 收窄防回吐"""
    atr_pct = atr / price * 100 if price else 1.0
    if atr_pct >= 1.5:
        return {"stop": 2.0, "trail": 2.5, "regime": "高波动"}
    if atr_pct >= 0.7:
        return {"stop": 1.5, "trail": 2.0, "regime": "中波动"}
    return {"stop": 1.2, "trail": 1.5, "regime": "低波动"}


def _last_settlement(now):
    """最近一个资金费结算点（UTC 00/08/16 整点）的时间戳"""
    import datetime
    dt = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    day0 = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    cands = [day0 + datetime.timedelta(hours=h) for h in FUNDING_HOURS_UTC]
    cands.append(day0 - datetime.timedelta(hours=8))  # 前一日的 16 点
    past = [c.timestamp() for c in cands if c.timestamp() <= now]
    return max(past)


# ---------- 交易执行 ----------
def _exec_price(price, side, is_open):
    """含滑点的成交价：开仓做多/平仓做空 → 高价成交；反之低价"""
    slip = price * SLIP_PCT
    adverse = (side > 0) == is_open
    return price + slip if adverse else price - slip


def _open(symbol, side, price, score, atr, basis, positions, now):
    """风险定量开仓（GitHub 主流做法）：
    每笔最大亏损 = 权益 × _risk_pct；仓位 = 风险预算 ÷ (杠杆 × 止损距离%) 反推"""
    eq, _ = equity(_prices(), positions)
    risk_pct = _risk_pct(score, symbol)
    if risk_pct <= 0 or not atr or atr <= 0:
        return False
    exec_p = _exec_price(price, side, True)
    # 初始止损 1.5×ATR(4h)，20x 保护：不超过强平距离的 80%
    mmr = _mmr(symbol)
    liq_dist = exec_p * (1 / LEVERAGE - mmr)
    stop_dist = min(1.5 * atr, abs(liq_dist) * 0.8)
    stop_pct = stop_dist / exec_p
    if stop_pct <= 0:
        return False
    # 风险定量：止损触发时亏损 = margin × 杠杆 × stop_pct ≤ 权益 × risk_pct
    margin = eq * risk_pct / (LEVERAGE * stop_pct)
    # 上限：单仓保证金不超过权益 30%（防低波动合约算出天价仓位）
    margin = min(margin, eq * 0.30)
    avail_cap = eq * MARGIN_CAP - used_margin(positions)
    margin = min(margin, avail_cap, balance())
    if margin <= 5:
        return False
    notional = margin * LEVERAGE
    qty = notional / exec_p
    fee = notional * FEE_PCT
    stop = exec_p - side * stop_dist
    liq = _liq_price(exec_p, side, mmr)
    store.kv_set("paper_balance", balance() - margin - fee)
    store.execute(
        """INSERT INTO paper_positions(symbol,side,qty,avg_entry,margin,leverage,stop,target,
           adds,opened_ts,last_add_ts,open_score,basis,funding_acc,liq_price,funding_ts,
           atr_entry,peak_price,tp_done)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,0)""",
        (symbol, side, qty, exec_p, margin, LEVERAGE, stop, 0, 0, now, now, score,
         json.dumps({"score": score, "risk": f"{risk_pct*100:.1f}%", "tf": "4h",
                     "reasons": basis}, ensure_ascii=False),
         liq, now, atr, exec_p))
    print(f"[paper] 开仓 {'多' if side>0 else '空'} {symbol} 4h评分{int(score):+d} 风险{risk_pct*100:.1f}% "
          f"保证金{margin:.1f}(止损距{stop_pct*100:.2f}%) 价{exec_p:.4g} 止{stop:.4g} 强平{liq:.4g}")
    return True


def _add(p, price, score, atr, positions, now):
    """加仓：同样按风险定量，止损按新均价重算（含强平保护）"""
    eq, _ = equity(_prices(), positions)
    risk_pct = _risk_pct(score, p["symbol"])
    if risk_pct <= 0 or not atr or atr <= 0:
        return False
    exec_p = _exec_price(price, p["side"], True)
    mmr = _mmr(p["symbol"])
    add_qty_est = 0  # 占位（下方按新均价重算止损）
    # 先近似用当前价估算止损距离
    liq_dist = exec_p * (1 / LEVERAGE - mmr)
    stop_dist = min(1.5 * atr, abs(liq_dist) * 0.8)
    stop_pct = stop_dist / exec_p
    if stop_pct <= 0:
        return False
    margin = eq * risk_pct / (LEVERAGE * stop_pct)
    margin = min(margin, eq * 0.30, eq * MARGIN_CAP - used_margin(positions), balance())
    if margin <= 5:
        return False
    notional = margin * LEVERAGE
    add_qty = notional / exec_p
    fee = notional * FEE_PCT
    new_qty = p["qty"] + add_qty
    new_avg = (p["avg_entry"] * p["qty"] + exec_p * add_qty) / new_qty
    liq_dist = new_avg * (1 / LEVERAGE - mmr)
    stop_dist = min(1.5 * atr, abs(liq_dist) * 0.8)
    stop = new_avg - p["side"] * stop_dist
    liq = _liq_price(new_avg, p["side"], mmr)
    store.kv_set("paper_balance", balance() - margin - fee)
    store.execute(
        """UPDATE paper_positions SET qty=?, avg_entry=?, margin=?, stop=?, liq_price=?,
           atr_entry=?, adds=?, last_add_ts=?, open_score=? WHERE id=?""",
        (new_qty, new_avg, p["margin"] + margin, stop, liq, atr,
         p["adds"] + 1, now, score, p["id"]))
    print(f"[paper] 加仓 {p['symbol']} 第{p['adds']+1}次 评分{int(score):+d} 新均价{new_avg:.4g} 新止损{stop:.4g} 新强平{liq:.4g}")
    return True


def _close(p, price, reason, now):
    exec_p = _exec_price(price, p["side"], False)
    notional = p["qty"] * exec_p
    fee = notional * FEE_PCT
    pnl = (exec_p - p["avg_entry"]) * p["qty"] * p["side"] - p["funding_acc"]
    # 破产保护：单笔亏损不超过该仓保证金（逐仓模式，损失以保证金为限）
    ret = max(p["margin"] + pnl - fee, 0.0)
    store.kv_set("paper_balance", balance() + ret)
    store.execute("DELETE FROM paper_positions WHERE id=?", (p["id"],))
    store.execute(
        """INSERT INTO paper_trades(symbol,side,qty,entry_price,exit_price,margin,pnl,fee,funding,
           reason,open_score,opened_ts,closed_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (p["symbol"], p["side"], p["qty"], p["avg_entry"], exec_p, p["margin"],
         round(max(pnl, -p["margin"]), 4), round(fee + p["qty"] * p["avg_entry"] * FEE_PCT, 4),
         round(p["funding_acc"], 4), reason, p["open_score"], p["opened_ts"], now))
    tag = "盈" if pnl > 0 else "亏"
    print(f"[paper] 平仓[{reason}] {p['symbol']} {'多' if p['side']>0 else '空'} "
          f"{p['avg_entry']:.4g}→{exec_p:.4g} {tag}{abs(max(pnl, -p['margin'])):.2f}U")


def _partial_close(p, price, fraction, reason, now):
    """C·分批止盈：平掉部分仓位，落袋对应保证金+盈亏"""
    qty_part = p["qty"] * fraction
    margin_part = p["margin"] * fraction
    exec_p = _exec_price(price, p["side"], False)
    fee = qty_part * exec_p * FEE_PCT
    pnl = (exec_p - p["avg_entry"]) * qty_part * p["side"]
    ret = max(margin_part + pnl - fee, 0.0)
    store.kv_set("paper_balance", balance() + ret)
    store.execute("UPDATE paper_positions SET qty=?, margin=?, tp_done=1 WHERE id=?",
                  (p["qty"] - qty_part, p["margin"] - margin_part, p["id"]))
    store.execute(
        """INSERT INTO paper_trades(symbol,side,qty,entry_price,exit_price,margin,pnl,fee,funding,
           reason,open_score,opened_ts,closed_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (p["symbol"], p["side"], round(qty_part, 6), p["avg_entry"], exec_p, round(margin_part, 4),
         round(max(pnl, -margin_part), 4), round(fee, 4), 0.0,
         reason, p["open_score"], p["opened_ts"], now))
    print(f"[paper] 分批止盈 {p['symbol']} 平掉{int(fraction*100)}% @ {exec_p:.4g} 落袋{pnl:+.2f}U，剩余仓位移动止损跟随")
    return exec_p


def _liquidate(p, price, now):
    """强平：损失该仓全部保证金（币安逐仓强平机制，保证金没收，含保险基金费）"""
    store.execute("DELETE FROM paper_positions WHERE id=?", (p["id"],))
    store.execute(
        """INSERT INTO paper_trades(symbol,side,qty,entry_price,exit_price,margin,pnl,fee,funding,
           reason,open_score,opened_ts,closed_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (p["symbol"], p["side"], p["qty"], p["avg_entry"], p["liq_price"] or price, p["margin"],
         round(-p["margin"], 4), 0.0, round(p["funding_acc"], 4),
         "强平爆仓", p["open_score"], p["opened_ts"], now))
    print(f"[paper] ⚠️ 强平 {p['symbol']} {'多' if p['side']>0 else '空'} 触及强平价 {p['liq_price']:.4g}，"
          f"损失全部保证金 {p['margin']:.2f}U")
    # 强平冷静期：该合约 30 分钟内禁止再开仓，避免暴跌中反复开→爆循环
    store.kv_set(f"paper_liq_cd:{p['symbol']}", now)


# ---------- 主循环 ----------
def tick():
    """每 5s：资金费结算 → 平仓检查（强平优先）→ 开仓/加仓"""
    init()
    now = int(time.time())
    prices = _prices()
    if not prices:
        return
    positions = open_positions()

    # 资金费结算（币安官方机制：UTC 00/08/16 整点对跨结算点的持仓一次性收取）
    settle = _last_settlement(now)
    for p in positions:
        if (p["funding_ts"] or p["opened_ts"]) >= settle:
            continue  # 开仓后尚未跨过新结算点
        rate = _funding_rate(p["symbol"])
        if rate:
            # 资金费 = 名义价值 × 费率；多头付正费率（空头收），空头付负费率
            cost = p["qty"] * prices.get(p["symbol"], p["avg_entry"]) * rate * p["side"]
            if cost:
                store.execute("UPDATE paper_positions SET funding_acc = funding_acc + ?, funding_ts=? WHERE id=?",
                              (round(cost, 6), settle, p["id"]))
                print(f"[paper] 资金费结算 {p['symbol']} {'付' if cost > 0 else '收'}{abs(cost):.3f}U（费率{rate*100:.4f}%）")
        else:
            store.execute("UPDATE paper_positions SET funding_ts=? WHERE id=?", (settle, p["id"]))

    # 1) 平仓检查（强平最优先——与币安风控顺序一致）
    for p in list(positions):
        price = prices.get(p["symbol"])
        if not price:
            continue
        # 强平检查（旧仓位无 liq_price 时现算并回填）
        liq = p["liq_price"]
        if not liq:
            liq = _liq_price(p["avg_entry"], p["side"], _mmr(p["symbol"]))
            store.execute("UPDATE paper_positions SET liq_price=? WHERE id=?", (liq, p["id"]))
        if (p["side"] > 0 and price <= liq) or (p["side"] < 0 and price >= liq):
            _liquidate(p, price, now)
            positions = open_positions()
            continue
        score, atr, _ = _signal_4h(p["symbol"])
        reason = None
        if p["side"] > 0 and p["stop"] and price <= p["stop"]:
            reason = "止损"
        elif p["side"] < 0 and p["stop"] and price >= p["stop"]:
            reason = "止损"
        elif p["side"] > 0 and p["target"] and price >= p["target"]:
            reason = "止盈"
        elif p["side"] < 0 and p["target"] and price <= p["target"]:
            reason = "止盈"
        elif now - p["opened_ts"] >= TIMEOUT_SEC:
            reason = "超时48h"
        elif score is not None and ((p["side"] > 0 and score <= -ENTRY_TH) or (p["side"] < 0 and score >= ENTRY_TH)):
            reason = f"反向信号({int(score):+d})"
        else:
            # SuperTrend(10,3) 翻转出场（让利润奔跑，趋势终结才离场）
            kl4 = store.get_klines(p["symbol"], "4h", 60)
            if len(kl4) > 15:
                st_dir = ind.supertrend([k["high"] for k in kl4], [k["low"] for k in kl4], [k["close"] for k in kl4])
                if st_dir is not None and st_dir != p["side"]:
                    reason = "SuperTrend翻转"
        if reason:
            _close(p, price, reason, now)
            positions = open_positions()

    # 2) 开仓 / 加仓（回测定案：4h评分±75）
    for symbol in tradfi_symbols():
        price = prices.get(symbol)
        if not price:
            continue
        score, atr, basis = _signal_4h(symbol)
        if score is None or abs(score) < ENTRY_TH:
            continue
        side = 1 if score > 0 else -1
        # ①长趋势过滤（GitHub 回测验证：期望 +69→+181U/笔的关键之一）：
        # 价格在 MA150(4h)≈25日线上方才做多，下方才做空
        kl4 = store.get_klines(symbol, "4h", 160)
        if len(kl4) < 150:
            continue
        ma150 = sum(k["close"] for k in kl4[-150:]) / 150
        if (side > 0 and price < ma150) or (side < 0 and price > ma150):
            continue
        existing = next((p for p in positions if p["symbol"] == symbol), None)
        if existing:
            # 加仓：同向、信号更强、次数未满
            if (existing["side"] == side and existing["adds"] < MAX_ADDS
                    and abs(score) > abs(existing["open_score"])):
                if _add(existing, price, score, atr, positions, now):
                    positions = open_positions()
        else:
            if len(positions) >= MAX_POSITIONS:
                continue
            # 强平冷静期：30 分钟内被强平过的合约不再开仓
            if now - float(store.kv_get(f"paper_liq_cd:{symbol}", 0)) < 1800:
                continue
            if _open(symbol, side, price, score, atr, basis, positions, now):
                positions = open_positions()

    # 3) 权益快照（每 60s）
    last_snap = int(store.kv_get("paper_snap_ts", 0))
    if now - last_snap >= 60:
        eq, _ = equity(prices, positions)
        store.execute("INSERT OR REPLACE INTO paper_equity(ts,equity,balance) VALUES(?,?,?)",
                      (now - now % 60, round(eq, 2), round(balance(), 2)))
        store.kv_set("paper_snap_ts", now)


def paper_loop():
    init()
    print(f"[paper] 模拟盘已启动：{START_BALANCE:.0f}U / {LEVERAGE}x / 最多{MAX_POSITIONS}仓 / 每5s决策")
    while True:
        try:
            tick()
        except Exception as e:
            print("[paper] tick 异常:", e)
        time.sleep(5)


# ---------- API 数据 ----------
def api_data():
    init()
    now = int(time.time())
    prices = _prices()
    positions = open_positions()
    eq, upnl_total = equity(prices, positions)
    bal = balance()
    um = used_margin(positions)

    pos_out = []
    for p in positions:
        price = prices.get(p["symbol"], p["avg_entry"])
        upnl = upnl_of(p, price)
        pnl_pct = upnl / p["margin"] * 100 if p["margin"] else 0
        hold_h = (now - p["opened_ts"]) / 3600
        basis = json.loads(p["basis"] or "{}")
        pos_out.append({
            "id": p["id"], "symbol": p["symbol"], "side": p["side"],
            "qty": round(p["qty"], 4), "avg_entry": p["avg_entry"], "price": price,
            "margin": round(p["margin"], 2), "leverage": p["leverage"],
            "stop": p["stop"], "target": p["target"], "tp_done": bool(p["tp_done"]),
            "liq_price": p["liq_price"],
            "liq_dist": round(abs(price - (p["liq_price"] or price)) / price * 100, 2) if price else None,
            "upnl": round(upnl, 2), "pnl_pct": round(pnl_pct, 1),
            "adds": p["adds"], "hold_h": round(hold_h, 1),
            "open_score": p["open_score"], "reasons": basis.get("reasons", []),
            "funding": round(p["funding_acc"], 3),
        })

    trades = [dict(r) for r in store.query(
        "SELECT * FROM paper_trades ORDER BY closed_ts DESC LIMIT 100")]

    # 统计
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / n * 100 if n else 0
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_w / gross_l if gross_l > 0 else (None if gross_w == 0 else 99.9)
    realized = sum(t["pnl"] - t["fee"] for t in trades)
    curve_rows = store.query("SELECT ts, equity FROM paper_equity ORDER BY ts DESC LIMIT 2880")
    curve = [(r["ts"], r["equity"]) for r in reversed(curve_rows)]
    # 最大回撤
    mdd = 0.0
    peak = START_BALANCE
    for _, e in curve:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak * 100)
    today0 = now - now % 86400 - 8 * 3600 if False else now - (now % 86400)  # 简化：UTC 日界
    today_trades = [t for t in trades if t["closed_ts"] >= today0]
    today_pnl = sum(t["pnl"] - t["fee"] for t in today_trades) + upnl_total

    return {
        "account": {
            "start": START_BALANCE, "balance": round(bal, 2), "equity": round(eq, 2),
            "used_margin": round(um, 2), "margin_usage": round(um / eq * 100, 1) if eq else 0,
            "upnl": round(upnl_total, 2), "today_pnl": round(today_pnl, 2),
            "ret_pct": round((eq - START_BALANCE) / START_BALANCE * 100, 2),
            "leverage": LEVERAGE, "max_positions": MAX_POSITIONS,
            "started_ts": store.kv_get("paper_start_ts", now),
        },
        "positions": pos_out,
        "trades": trades[:50],
        "curve": curve[-1440:],
        "stats": {
            "trades": n, "win_rate": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2) if profit_factor else None,
            "max_drawdown": round(mdd, 2), "realized": round(realized, 2),
            "wins": len(wins), "losses": len(losses),
        },
        "rules": {
            "tiers": "4h评分±60直接开仓 + 25日线趋势过滤 / 风险定量：60-90→1.5%风险, 90+共振→2.5%",
            "exits": "强平(官方公式) / 1.5×ATR(4h)初始止损 / SuperTrend(10,3)翻转出场 / 反向4h信号±60 / 48h超时",
            "costs": "开平仓各0.05%手续费(taker) + 0.03%滑点 + 资金费UTC 00/08/16整点结算",
            "limits": f"逐仓{LEVERAGE}x / 最多{MAX_POSITIONS}仓 / 单合约最多加仓{MAX_ADDS}次 / 保证金上限80% / 单仓≤权益30%",
        },
        "disclaimer": "模拟盘按币安U本位永续官方规则建模（逐仓/强平/资金费/taker费），仅用于策略验证与学习，不构成投资建议。强平与止盈止损在实盘以标记价格触发，此处用最新价近似。",
    }
