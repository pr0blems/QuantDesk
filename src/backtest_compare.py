"""出场策略多方案 × 入场过滤 回测对比（相同信号与成本假设，bar 级模拟）"""
import sys
sys.path.insert(0, ".")
from quantdesk import store, signals, indicators as ind

MARGIN, LEV, COST, TIMEOUT_BARS = 1000.0, 10, 0.0016, 12
SYMBOLS = ["TSLAUSDT","NVDAUSDT","AAPLUSDT","AMDUSDT","MSFTUSDT","METAUSDT","COINUSDT","MSTRUSDT",
           "PLTRUSDT","CRWVUSDT","IRENUSDT","RKLBUSDT","HOODUSDT","BABAUSDT","TSMUSDT",
           "XAUUSDT","XAGUSDT","CLUSDT","COPPERUSDT","SOXLUSDT"]

def mult_of(atr, price):
    p = atr / price * 100 if price else 1.0
    if p >= 1.5: return 2.0, 2.5
    if p >= 0.7: return 1.5, 2.0
    return 1.2, 1.5

def simulate(kl, side, i, mode):
    """mode: old / adapt / adapt_partial / be_trail / full(ABC)"""
    entry = kl[i]["close"]
    atr = ind.atr([k["high"] for k in kl[:i+1]], [k["low"] for k in kl[:i+1]], [k["close"] for k in kl[:i+1]])
    if not atr or atr <= 0: return None
    qty = MARGIN * LEV / entry
    cost = MARGIN * LEV * COST
    sm, tm = mult_of(atr, entry)
    stop = entry - side * (1.5 if mode == "old" else sm) * atr
    target = entry + side * 2.5 * atr
    peak, be, tp = entry, False, False
    pnl = 0.0
    for j in range(i + 1, min(i + 1 + TIMEOUT_BARS, len(kl))):
        h, l = kl[j]["high"], kl[j]["low"]
        fav = (h - entry) * side if side > 0 else (entry - l)
        peak = max(peak, h) if side > 0 else min(peak, l)
        hit_stop = (l <= stop) if side > 0 else (h >= stop)
        if hit_stop:
            return pnl + (stop - entry) * qty * side - cost * (0.5 if tp else 1)
        if mode == "old" and ((side > 0 and h >= target) or (side < 0 and l <= target)):
            return pnl + (target - entry) * qty * side - cost
        if mode in ("adapt_partial", "full") and not tp and fav >= 1.5 * atr:
            pnl += 1.5 * atr * qty * 0.5 - cost / 2; qty *= 0.5; tp = True; be = True
            if mode == "adapt_partial":
                stop = entry  # 半仓后保本，剩余奔固定目标
        if mode in ("be_trail", "full") and not be and fav >= 1.0 * atr:
            stop = max(stop, entry) if side > 0 else min(stop, entry); be = True
        if mode in ("be_trail", "full") and be:
            t = peak - side * tm * atr
            stop = max(stop, t) if side > 0 else min(stop, t)
        if mode in ("adapt", "adapt_partial") and ((side > 0 and h >= target) or (side < 0 and l <= target)):
            return pnl + (target - entry) * qty * side - cost * (0.5 if tp else 1)
    exit_p = kl[min(i + TIMEOUT_BARS, len(kl) - 1)]["close"]
    return pnl + (exit_p - entry) * qty * side - cost * (0.5 if tp else 1)

def run(mode, min_score=60, trend_filter=False):
    trades = []
    for sym in SYMBOLS:
        kl = store.get_klines(sym, "1h", 300)
        if len(kl) < 120: continue
        i = 60
        while i < len(kl) - TIMEOUT_BARS - 1:
            score, _ = signals.score_klines(kl[:i + 1])
            if score is None or abs(score) < min_score:
                i += 1; continue
            side = 1 if score > 0 else -1
            if trend_filter:
                closes = [k["close"] for k in kl[:i + 1]]
                ma50 = sum(closes[-50:]) / 50
                if (side > 0 and closes[-1] < ma50) or (side < 0 and closes[-1] > ma50):
                    i += 1; continue
            pnl = simulate(kl, side, i, mode)
            if pnl is not None:
                trades.append(pnl)
            i += TIMEOUT_BARS + 1
    return trades

def report(name, trades):
    n = len(trades)
    if not n:
        print(f"{name:<38} 无交易"); return 0
    w = [t for t in trades if t > 0]
    total = sum(trades)
    print(f"{name:<38} {n:>3}笔 | 总盈亏 {total:>+6.0f}U | 胜率 {len(w)/n*100:>3.0f}% | 期望 {total/n:>+6.1f}U/笔")
    return total

print("—— 出场方案对比（入场阈值 ±60，无过滤）——")
report("旧: 固定1.5/2.5×ATR", run("old"))
report("B: 自适应止损+固定2.5×ATR目标", run("adapt"))
report("B+C: 自适应+分批止盈+固定目标", run("adapt_partial"))
report("A+B: 保本+吊灯移动止损(无止盈)", run("be_trail"))
report("A+B+C: 保本+移动+分批(原方案)", run("full"))
print()
print("—— 入场过滤对比（出场用 B+C 结构）——")
report("阈值±60 无过滤", run("adapt_partial", 60, False))
report("阈值±75", run("adapt_partial", 75, False))
report("阈值±60 + MA50顺势过滤", run("adapt_partial", 60, True))
report("阈值±75 + MA50顺势过滤", run("adapt_partial", 75, True))
print()
print("—— 旧出场 + 入场过滤（对照）——")
report("旧出场 阈值±60", run("old", 60, False))
report("旧出场 阈值±75 + MA50顺势", run("old", 75, True))
