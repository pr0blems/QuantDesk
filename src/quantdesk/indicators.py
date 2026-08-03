"""纯 Python 技术指标（无第三方依赖）：SMA/EMA/RSI/MACD/ATR/布林/OBV/SuperTrend"""

def sma(vals, n):
    if len(vals) < n: return None
    return sum(vals[-n:]) / n

def sma_series(vals, n):
    out = []
    for i in range(len(vals)):
        if i + 1 < n: out.append(None)
        else: out.append(sum(vals[i+1-n:i+1]) / n)
    return out

def ema_series(vals, n):
    if not vals: return []
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out

def rsi(closes, n=14):
    if len(closes) < n + 1: return None
    gains, losses = [], []
    for i in range(-n, 0):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains) / n; al = sum(losses) / n
    if al == 0: return 100.0
    return 100 - 100 / (1 + ag / al)

def macd(closes, fast=12, slow=26, signal=9):
    """返回 (dif, dea, hist)；数据不足返回 (None,None,None)"""
    if len(closes) < slow + signal: return None, None, None
    ef = ema_series(closes, fast); es = ema_series(closes, slow)
    dif_series = [a - b for a, b in zip(ef, es)]
    dea_series = ema_series(dif_series[slow-1:], signal)
    dif = dif_series[-1]; dea = dea_series[-1]
    return dif, dea, (dif - dea) * 2

def atr(highs, lows, closes, n=14):
    if len(closes) < n + 1: return None
    trs = []
    for i in range(-n, 0):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i-1]),
                       abs(lows[i] - closes[i-1])))
    return sum(trs) / n

def bollinger(closes, n=20, mult=2.0):
    if len(closes) < n: return None, None, None
    seg = closes[-n:]
    mid = sum(seg) / n
    var = sum((x - mid) ** 2 for x in seg) / n
    sd = var ** 0.5
    return mid - mult * sd, mid, mid + mult * sd

def obv(closes, volumes):
    out = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i-1]: out.append(out[-1] + volumes[i])
        elif closes[i] < closes[i-1]: out.append(out[-1] - volumes[i])
        else: out.append(out[-1])
    return out

def supertrend(highs, lows, closes, n=10, mult=3.0):
    """标准 SuperTrend。返回当前方向：1 多头 / -1 空头；None 数据不足"""
    length = len(closes)
    if length < n + 2: return None
    # Wilder ATR 序列
    atrs = [None] * length
    tr_sum = 0.0
    for i in range(1, length):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        if i <= n:
            tr_sum += tr
            if i == n: atrs[i] = tr_sum / n
        else:
            atrs[i] = (atrs[i-1] * (n - 1) + tr) / n
    final_upper = final_lower = None
    direction = 1
    for i in range(n, length):
        if atrs[i] is None: continue
        hl2 = (highs[i] + lows[i]) / 2
        bu, bl = hl2 + mult * atrs[i], hl2 - mult * atrs[i]
        if final_upper is None:
            final_upper, final_lower = bu, bl
            direction = 1 if closes[i] > hl2 else -1
            continue
        final_upper = bu if (bu < final_upper or closes[i-1] > final_upper) else final_upper
        final_lower = bl if (bl > final_lower or closes[i-1] < final_lower) else final_lower
        if direction == 1 and closes[i] < final_lower:
            direction = -1
        elif direction == -1 and closes[i] > final_upper:
            direction = 1
    return direction
