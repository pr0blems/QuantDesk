"""多因子多空评分引擎：4 流派投票 + 波动率状态，每票附中文理由，输出 -100~+100
原则：任何情况下 8 个分析项都必须出现，数据不足时明确说明，绝不空缺。"""
from . import indicators as ind

FACTOR_WEIGHTS = {
    "ma_trend": 20,      # 趋势：均线排列
    "macd": 15,          # 趋势：MACD
    "supertrend": 15,    # 趋势：SuperTrend
    "rsi": 15,           # 回归：RSI
    "bollinger": 10,     # 回归：布林 %B
    "obv": 15,           # 量价：OBV
    "volume": 10,        # 量价：放量确认
    "atr_regime": 0,     # 波动：ATR 状态（信息项，不计分）
}

MIN_CANDLES = 25  # 最低评分数据量；不足40根时部分长周期因子会标注"数据不足"

def _insufficient(name, key, need, have):
    return dict(key=key, name=name, vote=0, points=0.0,
                reason=f"数据不足：该指标需要至少 {need} 根K线，当前仅 {have} 根（新上市品种，随时间自动补齐）")

def score_klines(kl):
    """kl: list of dict(open_time,open,high,low,close,volume) 按时间升序。
    返回 (score, factors) factors 固定 8 项"""
    if len(kl) < MIN_CANDLES:
        return None, []
    n = len(kl)
    closes = [k["close"] for k in kl]
    highs = [k["high"] for k in kl]
    lows = [k["low"] for k in kl]
    vols = [k["volume"] for k in kl]
    c = closes[-1]
    factors = []

    # 1. 均线排列 MA20 vs MA50
    if n >= 50:
        ma20, ma50 = ind.sma(closes, 20), ind.sma(closes, 50)
        diff_pct = (ma20 - ma50) / ma50 * 100
        pos20 = (c - ma20) / ma20 * 100
        if ma20 > ma50 and c > ma20:
            vote, pts = 1, 1.0
            reason = f"MA20({ma20:.4g}) 在 MA50({ma50:.4g}) 上方 {diff_pct:.2f}%，现价高于 MA20 {pos20:.2f}%，标准多头排列"
        elif ma20 > ma50:
            vote, pts = 1, 0.5
            reason = f"MA20 在 MA50 上方 {diff_pct:.2f}%，但现价低于 MA20 {abs(pos20):.2f}%，多头排列出现松动"
        elif ma20 < ma50 and c < ma20:
            vote, pts = -1, 1.0
            reason = f"MA20 在 MA50 下方 {abs(diff_pct):.2f}%，现价低于 MA20 {abs(pos20):.2f}%，标准空头排列"
        else:
            vote, pts = -1, 0.5
            reason = f"MA20 在 MA50 下方 {abs(diff_pct):.2f}%，但现价反弹至 MA20 上方 {pos20:.2f}%，空头排列出现松动"
        factors.append(dict(key="ma_trend", name="均线排列", vote=vote, points=pts, reason=reason))
    else:
        factors.append(_insufficient("均线排列", "ma_trend", 50, n))

    # 2. MACD
    dif, dea, hist = ind.macd(closes)
    if hist is not None:
        if hist > 0 and dif > 0:
            vote, pts = 1, 1.0
            reason = f"DIF({dif:.4g}) 在零轴上方且柱体为正({hist:.4g})，上涨动能延续"
        elif hist > 0:
            vote, pts = 1, 0.5
            reason = f"DIF({dif:.4g}) 仍在零轴下方，但柱体转正({hist:.4g})，下跌动能收敛、有金叉迹象"
        elif hist < 0 and dif < 0:
            vote, pts = -1, 1.0
            reason = f"DIF({dif:.4g}) 在零轴下方且柱体为负({hist:.4g})，下跌动能延续"
        else:
            vote, pts = -1, 0.5
            reason = f"DIF({dif:.4g}) 仍在零轴上方，但柱体转负({hist:.4g})，上涨动能衰减、有死叉迹象"
        factors.append(dict(key="macd", name="MACD", vote=vote, points=pts, reason=reason))
    else:
        factors.append(_insufficient("MACD", "macd", 35, n))

    # 3. SuperTrend
    st = ind.supertrend(highs, lows, closes)
    if st is not None:
        vote, pts = st, 1.0
        reason = "SuperTrend(10,3) 为多头方向，价格运行在趋势线上方，趋势止损线提供下方支撑" if st > 0 \
            else "SuperTrend(10,3) 为空头方向，价格运行在趋势线下方，趋势止损线构成上方压力"
        factors.append(dict(key="supertrend", name="SuperTrend", vote=vote, points=pts, reason=reason))
    else:
        factors.append(_insufficient("SuperTrend", "supertrend", 12, n))

    # 4. RSI
    r = ind.rsi(closes)
    if r is not None:
        if r >= 70:
            vote, pts = -1, 0.8
            reason = f"RSI(14)={r:.1f} ≥70 进入超买区，短线获利回吐压力大，追高风险高"
        elif r >= 55:
            vote, pts = 1, 0.6
            reason = f"RSI(14)={r:.1f} 位于 55~70 强势区，买方力量占优且尚未过热"
        elif r <= 30:
            vote, pts = 1, 0.8
            reason = f"RSI(14)={r:.1f} ≤30 进入超卖区，恐慌情绪释放充分，短线反弹概率上升"
        elif r <= 45:
            vote, pts = -1, 0.6
            reason = f"RSI(14)={r:.1f} 位于 30~45 弱势区，卖方力量占优但尚未超卖"
        else:
            vote, pts = 0, 0.0
            reason = f"RSI(14)={r:.1f} 位于 45~55 中性区，多空力量暂时均衡，等待方向选择"
        factors.append(dict(key="rsi", name="RSI", vote=vote, points=pts, reason=reason))
    else:
        factors.append(_insufficient("RSI", "rsi", 15, n))

    # 5. 布林带 %B（永远给出位置读数 + 收窄预警）
    if n >= 20:
        bl, bm, bu = ind.bollinger(closes)
        pctb = (c - bl) / (bu - bl) if bu > bl else 0.5
        width = (bu - bl) / bm * 100
        squeeze = "；⚠️ 带宽仅 " + f"{width:.1f}%" + "，布林带明显收窄，变盘临近，重点盯突破方向" if width < 4 else ""
        if pctb > 1.0:
            vote, pts = -1, 0.7
            reason = f"%B={pctb:.2f}，价格突破上轨({bu:.4g})，短线偏离均值过远，警惕冲高回落{squeeze}"
        elif pctb >= 0.8:
            vote, pts = 1, 0.6
            reason = f"%B={pctb:.2f}，价格贴近上轨({bu:.4g})运行，处于强势区（带宽 {width:.1f}%）{squeeze}"
        elif pctb >= 0.45:
            vote, pts = 0, 0.0
            reason = f"%B={pctb:.2f}，价格在中轨({bm:.4g})附近，区间内震荡、方向不明（带宽 {width:.1f}%）{squeeze}"
        elif pctb >= 0.2:
            vote, pts = 0, 0.0
            reason = f"%B={pctb:.2f}，价格在中轨下方运行，走势偏弱但未达极端（带宽 {width:.1f}%）{squeeze}"
        elif pctb >= 0:
            vote, pts = -1, 0.6
            reason = f"%B={pctb:.2f}，价格贴近下轨({bl:.4g})，处于弱势区（带宽 {width:.1f}%）{squeeze}"
        else:
            vote, pts = 1, 0.7
            reason = f"%B={pctb:.2f}，价格跌破下轨({bl:.4g})，偏离均值过远，关注超跌反抽{squeeze}"
        factors.append(dict(key="bollinger", name="布林带%B", vote=vote, points=pts, reason=reason))
    else:
        factors.append(_insufficient("布林带%B", "bollinger", 20, n))

    # 6. OBV 资金流（永远给出读数）
    obvs = ind.obv(closes, vols)
    if len(obvs) >= 25:
        obv_now = sum(obvs[-5:]) / 5
        obv_prev = sum(obvs[-20:-5]) / 15
        vol_scale = sum(vols[-20:]) / 20 or 1
        slope = (obv_now - obv_prev) / vol_scale
        price_chg = (closes[-1] - closes[-20]) / closes[-20] * 100
        if slope > 0.3:
            vote, pts = 1, 1.0
            reason = f"OBV 近5根均值较前15根净流入 {slope:.1f} 倍均量，资金持续流入（同期价格 {price_chg:+.2f}%）"
        elif slope < -0.3:
            vote, pts = -1, 1.0
            reason = f"OBV 近5根均值较前15根净流出 {abs(slope):.1f} 倍均量，资金持续流出（同期价格 {price_chg:+.2f}%）"
        else:
            vote, pts = (1 if slope > 0 else -1 if slope < 0 else 0), 0.3 if slope != 0 else 0.0
            reason = f"OBV 变化平缓（{slope:+.1f} 倍均量），资金面{'小幅流入' if slope > 0 else '小幅流出' if slope < 0 else '持平'}，暂无明确方向"
        if slope > 0.3 and price_chg < -1:
            reason += "；★ 价跌量增，疑似底背离，留意反转"
        elif slope < -0.3 and price_chg > 1:
            reason += "；★ 价涨量缩，疑似顶背离，留意回落"
        factors.append(dict(key="obv", name="OBV资金流", vote=vote, points=pts, reason=reason))
    else:
        factors.append(_insufficient("OBV资金流", "obv", 25, n))

    # 7. 成交量确认（永远给出统计读数）
    if len(vols) >= 21:
        v_now = vols[-1]
        v_avg = sum(vols[-21:-1]) / 20
        v_base = v_avg or 1
        ratio = v_now / v_base
        bar_up = closes[-1] >= kl[-1]["open"]
        stats = f"最新K线成交量 {v_now:.4g}，20期均量 {v_avg:.4g}，比值 {ratio:.2f} 倍"
        if v_avg == 0:
            vote, pts = 0, 0.0
            reason = f"{stats}；该品种近期成交极度清淡，量价信号参考价值低，建议以趋势类指标为主"
        elif ratio >= 2.0:
            vote, pts = (1 if bar_up else -1), 1.0
            reason = f"{stats}；显著放量且收{'阳线' if bar_up else '阴线'}，{'多方' if bar_up else '空方'}力量集中释放，信号可信度高"
        elif ratio >= 1.3:
            vote, pts = (1 if bar_up else -1), 0.5
            reason = f"{stats}；温和放量且收{'阳线' if bar_up else '阴线'}，对{'多头' if bar_up else '空头'}方向略有支撑"
        else:
            vote, pts = 0, 0.0
            reason = f"{stats}；量能{'偏低' if ratio < 0.6 else '正常'}，当前价格变化缺乏成交量确认，突破/反转信号可信度{'下降' if ratio < 0.6 else '一般'}"
        factors.append(dict(key="volume", name="成交量确认", vote=vote, points=pts, reason=reason))
    else:
        factors.append(_insufficient("成交量确认", "volume", 21, n))

    # 8. ATR 波动率状态（信息项，不计分）
    a = ind.atr(highs, lows, closes)
    if a is not None and c > 0:
        atr_pct = a / c * 100
        hist_a = [ind.atr(highs[:i], lows[:i], closes[:i]) for i in range(len(closes) - 20, len(closes))]
        hist_a = [x for x in hist_a if x]
        avg_a = sum(hist_a) / len(hist_a) if hist_a else a
        rel = a / avg_a if avg_a else 1
        if atr_pct >= 3:
            regime, advice = "高波动", "波动剧烈，务必缩小仓位、放宽止损"
        elif atr_pct >= 1.5:
            regime, advice = "中等波动", "正常仓位，按 ATR 止损执行即可"
        else:
            regime, advice = "低波动", "走势平静，警惕收窄后的突然变盘"
        reason = (f"ATR(14)={a:.4g}，约占现价 {atr_pct:.2f}%（{regime}，为近20期均值的 {rel:.1f} 倍）；"
                  f"按 1.5×ATR 止损 ≈ {1.5 * atr_pct:.2f}% 距离。{advice}")
        factors.append(dict(key="atr_regime", name="波动率状态", vote=0, points=0.0, reason=reason))
    else:
        factors.append(_insufficient("波动率状态", "atr_regime", 15, n))

    # 合成
    total_w = sum(FACTOR_WEIGHTS[f["key"]] for f in factors if FACTOR_WEIGHTS[f["key"]] > 0)
    raw = sum(f["vote"] * f["points"] * FACTOR_WEIGHTS[f["key"]] for f in factors)
    score = round(raw / total_w * 100) if total_w else 0
    for f in factors:
        w = FACTOR_WEIGHTS[f["key"]]
        f["weight"] = w
        f["contribution"] = round(f["vote"] * f["points"] * w / total_w * 100, 1) if (total_w and w) else 0
    return score, factors

def combined_score(per_tf):
    """per_tf: {tf: (score, factors)} 按配置权重合成"""
    from .config_loader import settings
    w = settings["timeframe_weights"]
    num, den = 0.0, 0.0
    for tf, val in per_tf.items():
        if val and val[0] is not None:
            num += val[0] * w.get(tf, 0.3)
            den += w.get(tf, 0.3)
    return round(num / den) if den else None
