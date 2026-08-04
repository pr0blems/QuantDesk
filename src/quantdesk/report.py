"""分析报告引擎：结论标签 + 1h/4h/12h 操作建议（方向/依据/新闻支撑/参考位）。"""

import json
import time

from . import indicators as ind
from . import store

# 标的 → 新闻匹配关键词（小写）
KEYWORDS = {
    "NVDA": ["nvidia", "英伟达"],
    "TSLA": ["tesla", "特斯拉"],
    "AAPL": ["apple", "苹果"],
    "MSFT": ["microsoft", "微软"],
    "META": ["meta", "facebook"],
    "AMD": ["amd"],
    "AMZN": ["amazon", "亚马逊"],
    "GOOGL": ["google", "alphabet", "谷歌"],
    "NFLX": ["netflix", "奈飞"],
    "COIN": ["coinbase"],
    "PLTR": ["palantir"],
    "MSTR": ["microstrategy", "strategy inc"],
    "TSM": ["tsmc", "台积电"],
    "BABA": ["alibaba", "阿里巴巴"],
    "AVGO": ["broadcom", "博通"],
    "ARM": ["arm holdings", " arm "],
    "ASML": ["asml"],
    "MU": ["micron", "美光"],
    "INTC": ["intel", "英特尔"],
    "QCOM": ["qualcomm", "高通"],
    "TXN": ["texas instruments"],
    "SONY": ["sony", "索尼"],
    "LLY": ["eli lilly", "礼来"],
    "JPM": ["jpmorgan", "摩根大通"],
    "GS": ["goldman", "高盛"],
    "UBER": ["uber", "优步"],
    "DIS": ["disney", "迪士尼"],
    "WMT": ["walmart", "沃尔玛"],
    "COST": ["costco"],
    "CRM": ["salesforce"],
    "CRWD": ["crowdstrike"],
    "PANW": ["palo alto"],
    "SNOW": ["snowflake"],
    "CRWV": ["coreweave"],
    "IREN": ["iren"],
    "CRCL": ["circle"],
    "HOOD": ["robinhood"],
    "RKLB": ["rocket lab"],
    "SNDK": ["sandisk"],
    "SMCI": ["supermicro", "超微"],
    "DELL": ["dell", "戴尔"],
    "IBM": ["ibm"],
    "CSCO": ["cisco", "思科"],
    "ORCL": ["oracle", "甲骨文"],
    "ADBE": ["adobe"],
    "PYPL": ["paypal"],
    "PAYP": ["paypal"],
    "EBAY": ["ebay"],
    "GME": ["gamestop"],
    "RIVN": ["rivian"],
    "SOFI": ["sofi"],
    "DKNG": ["draftkings"],
    "SAMSUNG": ["samsung", "三星"],
    "SKHYNIX": ["hynix", "海力士"],
    "SKHY": ["hynix", "海力士"],
    "HYUNDAI": ["hyundai", "现代"],
    "TENCENT": ["tencent", "腾讯"],
    "HK0700": ["tencent", "腾讯"],
    "HK1810": ["xiaomi", "小米"],
    "MINIMAX": ["minimax"],
    "ZHIPU": ["zhipu", "智谱"],
    "POPMART": ["pop mart", "泡泡玛特"],
    "OPENAI": ["openai"],
    "ANTHROPIC": ["anthropic"],
    "XAU": ["gold", "黄金", "金价"],
    "XAG": ["silver", "白银"],
    "XPT": ["platinum", "铂金"],
    "XPD": ["palladium", "钯金"],
    "CL": ["crude oil", " oil ", "原油", "wti"],
    "BZ": ["brent", "布伦特"],
    "COPPER": ["copper", "铜价", "精铜"],
    "NATGAS": ["natural gas", "天然气"],
    "SPY": ["s&p 500", "sp500", "标普"],
    "QQQ": ["nasdaq", "纳指", "纳斯达克"],
    "TQQQ": ["nasdaq", "纳指"],
    "SQQQ": ["nasdaq", "纳指"],
    "IWM": ["russell", "罗素"],
    "SOXL": ["semiconductor", "半导体", "chip"],
    "SOXS": ["semiconductor", "半导体"],
    "SMH": ["semiconductor", "半导体"],
    "EWJ": ["japan", "日本股市", "日经"],
    "EWY": ["south korea", "韩国股市"],
    "EWZ": ["brazil", "巴西"],
    "EWT": ["taiwan", "台湾股市"],
    "KORU": ["south korea", "韩国"],
    "XLE": ["energy sector", "能源股"],
    "URNM": ["uranium", "铀"],
    "BITO": ["bitcoin etf", "比特币etf"],
    "UVXY": ["vix", "波动率"],
    "TMF": ["treasury", "美债", "国债"],
    "TBT": ["treasury", "美债"],
    "TZA": ["russell", "罗素", "small cap"],
}
MACRO_KEYS = [
    "federal reserve",
    "fomc",
    "cpi",
    "inflation",
    "interest rate",
    "powell",
    "美联储",
    "通胀",
    "非农",
    "降息",
    "加息",
    " fed ",
    " fed's",
]


def conclusion_label(score):
    if score is None:
        return "数据不足", "#77808f"
    if score >= 75:
        return "强烈看多", "#2ebd85"
    if score >= 40:
        return "看多", "#7fc8a9"
    if score <= -75:
        return "强烈看空", "#f6465d"
    if score <= -40:
        return "看空", "#e98a97"
    return "中性观望", "#77808f"


def market_breadth():
    rows = store.query("""
        SELECT s.symbol, s.score FROM scores s
        JOIN (SELECT symbol, MAX(open_time) mo FROM scores WHERE tf='1h' GROUP BY symbol) m
        ON s.symbol=m.symbol AND s.open_time=m.mo WHERE s.tf='1h'""")
    bull = sum(1 for r in rows if r["score"] >= 40)
    bear = sum(1 for r in rows if r["score"] <= -40)
    neu = len(rows) - bull - bear
    if not rows:
        return {
            "bull": 0,
            "bear": 0,
            "neutral": 0,
            "total": 0,
            "conclusion": "数据收集中…",
            "color": "#77808f",
        }
    if bull > bear * 2 and bull >= 5:
        c, col = f"市场整体偏多（{bull}多 / {bear}空 / {neu}中性）", "#2ebd85"
    elif bear > bull * 2 and bear >= 5:
        c, col = f"市场整体偏空（{bull}多 / {bear}空 / {neu}中性）", "#f6465d"
    else:
        c, col = f"市场多空分歧（{bull}多 / {bear}空 / {neu}中性）", "#f0b90b"
    return {
        "bull": bull,
        "bear": bear,
        "neutral": neu,
        "total": len(rows),
        "conclusion": c,
        "color": col,
    }


def _tf_scores(symbol):
    rows = store.query(
        """
        SELECT s.tf, s.score, s.detail FROM scores s
        JOIN (SELECT tf, MAX(open_time) mo FROM scores WHERE symbol=? GROUP BY tf) m
        ON s.tf=m.tf AND s.open_time=m.mo WHERE s.symbol=?""",
        (symbol, symbol),
    )
    return {
        r["tf"]: {"score": r["score"], "factors": json.loads(r["detail"] or "[]")} for r in rows
    }


def _top_reasons(tf_data, tfs, n=4):
    cands = []
    for tf in tfs:
        d = tf_data.get(tf)
        if not d:
            continue
        for f in sorted(d["factors"], key=lambda x: -abs(x.get("contribution", 0))):
            cands.append(f"[{tf}·{f['name']}] {f['reason']}")
    # 附一条波动率状态（如有）
    for tf in tfs:
        d = tf_data.get(tf)
        if d:
            for f in d["factors"]:
                if f["key"] == "atr_regime" and f["weight"] == 0:
                    cands.append(f"[{tf}·波动率] {f['reason']}")
                    break
            break
    return cands[: n + 1]


def _match_news(symbol, limit=4, hours=48):
    base = symbol.replace("USDT", "").replace("USD1", "")
    kws = [k.lower() for k in KEYWORDS.get(base, [])]
    if not kws and len(base) >= 4:
        kws = [base.lower()]
    since = int(time.time()) - hours * 3600
    rows = store.query(
        "SELECT ts, source, lang, title, title_zh, link, sentiment FROM news WHERE ts>? ORDER BY ts DESC LIMIT 300",
        (since,),
    )
    hits, macro = [], []
    for r in rows:
        text = ((r["title"] or "") + " " + (r["title_zh"] or "")).lower()
        if kws and any(k in text for k in kws):
            hits.append(dict(r))
        elif any(m in text for m in MACRO_KEYS):
            macro.append(dict(r))
    out = hits[:limit]
    if len(out) < limit:
        for m in macro:
            m = dict(m)
            m["macro"] = True
            out.append(m)
            if len(out) >= limit:
                break
    return out, bool(hits)


def build_report(symbol):
    tf_data = _tf_scores(symbol)
    ticker = store.query("SELECT price, pct_24h FROM ticker WHERE symbol=?", (symbol,))
    price = ticker[0]["price"] if ticker else None
    pct = ticker[0]["pct_24h"] if ticker else None

    # 综合分
    w = {"15m": 0.3, "1h": 0.4, "4h": 0.3}
    num = sum(tf_data[t]["score"] * w[t] for t in w if t in tf_data)
    den = sum(w[t] for t in w if t in tf_data)
    combined = round(num / den) if den else None
    label, color = conclusion_label(combined)

    # ATR 参考位
    kl1h = store.get_klines(symbol, "1h", 60)
    kl4h = store.get_klines(symbol, "4h", 60)
    atr1h = (
        ind.atr([k["high"] for k in kl1h], [k["low"] for k in kl1h], [k["close"] for k in kl1h])
        if len(kl1h) > 15
        else None
    )
    atr4h = (
        ind.atr([k["high"] for k in kl4h], [k["low"] for k in kl4h], [k["close"] for k in kl4h])
        if len(kl4h) > 15
        else None
    )

    news, direct = _match_news(symbol)

    # 社交情绪（Stocktwits + Reddit/4chan）
    social = {}
    base = symbol.replace("USDT", "").replace("USD1", "")
    srows = store.query("SELECT * FROM social WHERE symbol=?", (base,))
    if srows:
        s = srows[0]
        total = (s["st_bull"] or 0) + (s["st_bear"] or 0)
        if total > 0:
            bull_pct = round((s["st_bull"] or 0) / total * 100)
            social["stocktwits"] = (
                f"🐂{bull_pct}% vs 🐻{100 - bull_pct}%（近{s['st_msgs']}条中{total}条带标签）"
            )
            social["st_bull_pct"] = bull_pct
        if s["ape_mentions"]:
            trend = ""
            if s["ape_rank"] and s["ape_rank_24h"]:
                diff = s["ape_rank_24h"] - s["ape_rank"]
                trend = f"，热度榜第{s['ape_rank']}名（较昨日{'↑' if diff > 0 else '↓' if diff < 0 else '='}{abs(diff)}位）"
            social["reddit"] = (
                f"Reddit/4chan 24h 提及 {s['ape_mentions']} 次、获赞 {s['ape_upvotes']}{trend}"
            )
    tr = store.system_state_get("st_trending", {})
    if base in (tr.get("symbols") or []):
        social["trending"] = "🔥 Stocktwits 热议榜在列"

    # 成交量统计（24h成交额 + 量能趋势）
    stats = {}
    tk = store.query("SELECT quote_volume FROM ticker WHERE symbol=?", (symbol,))
    if tk and tk[0]["quote_volume"]:
        qv = tk[0]["quote_volume"]
        stats["24h成交额"] = f"{qv / 1e6:.2f}M USDT" if qv >= 1e6 else f"{qv / 1e3:.1f}K USDT"
    if len(kl1h) >= 21:
        v_now = kl1h[-1]["volume"]
        v_avg = sum(k["volume"] for k in kl1h[-21:-1]) / 20
        if v_avg > 0:
            r = v_now / v_avg
            stats["1h量比"] = (
                f"{r:.2f} 倍（{'放量' if r >= 1.3 else '缩量' if r <= 0.6 else '正常'}）"
            )
        stats["1h最新量"] = f"{kl1h[-1]['volume']:.4g}"

    def fmt(v):
        if v is None:
            return "--"
        return f"{v:,.4g}"

    def horizon(name, tfs, weights, atr):
        sc_num = sum(
            tf_data[t]["score"] * wt for t, wt in zip(tfs, weights, strict=True) if t in tf_data
        )
        sc_den = sum(wt for t, wt in zip(tfs, weights, strict=True) if t in tf_data)
        hs = round(sc_num / sc_den) if sc_den else None
        if hs is None:
            return {
                "name": name,
                "suggestion": "数据不足",
                "color": "#77808f",
                "basis": ["K线数据回填中，暂无法给出该周期建议"],
                "levels": None,
                "news": news[:2],
            }
        if hs >= 40:
            sug, col, side = ("建议做多", "#2ebd85", 1) if hs >= 60 else ("轻仓偏多", "#7fc8a9", 1)
        elif hs <= -40:
            sug, col, side = (
                ("建议做空", "#f6465d", -1) if hs <= -60 else ("轻仓偏空", "#e98a97", -1)
            )
        else:
            sug, col, side = "观望为主", "#f0b90b", 0
        levels = None
        if price and atr and side != 0:
            entry = price
            stop = price - side * 1.5 * atr
            target = price + side * 2.5 * atr
            levels = {
                "参考入场": fmt(entry),
                "参考止损": fmt(stop)
                + f"（{side > 0 and '下' or '上'}方 1.5×ATR≈{abs(stop - entry) / entry * 100:.2f}%）",
                "参考目标": fmt(target)
                + f"（{side > 0 and '上' or '下'}方 2.5×ATR≈{abs(target - entry) / entry * 100:.2f}%）",
                "盈亏比": "约 1 : 1.67",
            }
        return {
            "name": name,
            "score": hs,
            "suggestion": sug,
            "color": col,
            "basis": _top_reasons(tf_data, tfs),
            "levels": levels,
            "news": news[:3],
        }

    horizons = [
        horizon("未来 1 小时", ["15m", "1h"], [0.5, 0.5], atr1h),
        horizon("未来 4 小时", ["1h", "4h"], [0.4, 0.6], atr4h),
        horizon("未来 12 小时", ["1h", "4h"], [0.25, 0.75], atr4h),
    ]
    return {
        "symbol": symbol,
        "price": price,
        "pct_24h": pct,
        "combined": combined,
        "label": label,
        "color": color,
        "tf_scores": {t: d["score"] for t, d in tf_data.items()},
        "horizons": horizons,
        "stats": stats,
        "social": social,
        "news_direct": direct,
        "disclaimer": "评分与建议基于技术指标统计与公开舆情，仅为概率倾向参考，不构成投资建议。",
    }
