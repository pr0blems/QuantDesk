"""Explainable, read-only strategy indicator scans for the monitor modal."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import indicators

STRATEGY_INDICATOR_COUNT = 12

BEARISH_STRATEGY_NAMES: dict[str, str] = {
    "bollinger_breakout": "布林跌破",
    "moving_average_pullback_bounce": "均线反弹受阻",
    "trend_breakout": "趋势跌破",
    "price_volume_rise": "量价齐跌",
    "new_low_reversal": "新高反转",
    "low_volume_pullback": "缩量反弹",
    "strong_gap_open": "强势低开",
    "moving_average_bull": "均线空头",
    "ma_golden_cross": "MA死叉",
    "macd_golden_cross_volume": "MACD死叉放量",
    "oversold_bounce": "超买回落",
    "oversold_reversal": "超买反转",
}


def _number(value: Any) -> float:
    return float(value)


def _price(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _percent(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:+.2f}%"


def _ratio(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.2f} 倍"


def _metric(label: str, value: str) -> dict[str, str]:
    return {"label": label, "value": value}


def _item(
    key: str,
    name: str,
    category: str,
    description: str,
    triggered: bool | None,
    summary: str,
    metrics: Sequence[dict[str, str]],
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "category": category,
        "description": description,
        "triggered": triggered,
        "status": (
            "insufficient" if triggered is None else "triggered" if triggered else "not_triggered"
        ),
        "summary": summary,
        "metrics": list(metrics),
    }


def _insufficient(
    key: str,
    name: str,
    category: str,
    description: str,
    need: int,
    have: int,
) -> dict[str, Any]:
    return _item(
        key,
        name,
        category,
        description,
        None,
        f"数据不足：至少需要 {need} 根 K 线，当前只有 {have} 根。",
        [_metric("需要 K 线", str(need)), _metric("当前 K 线", str(have))],
    )


def evaluate_strategy_indicators(
    candles: Sequence[Mapping[str, Any]], timeframe: str
) -> dict[str, Any]:
    """Evaluate twelve transparent observation indicators on ascending candles."""

    clean = [
        {
            "open_time": int(candle.get("open_time") or 0),
            "open": _number(candle["open"]),
            "high": _number(candle["high"]),
            "low": _number(candle["low"]),
            "close": _number(candle["close"]),
            "volume": _number(candle["volume"]),
        }
        for candle in candles
    ]
    count = len(clean)
    if not clean:
        items = [
            _insufficient(key, name, category, description, need, 0)
            for key, name, category, description, need in _definitions()
        ]
        return _result(timeframe, items, 0, None)

    opens = [item["open"] for item in clean]
    highs = [item["high"] for item in clean]
    lows = [item["low"] for item in clean]
    closes = [item["close"] for item in clean]
    volumes = [item["volume"] for item in clean]
    current_open, current_low, current_close = (
        opens[-1],
        lows[-1],
        closes[-1],
    )
    previous_close = closes[-2] if count >= 2 else None
    average_volume = sum(volumes[-21:-1]) / 20 if count >= 21 else None
    volume_ratio = (
        volumes[-1] / average_volume if average_volume is not None and average_volume > 0 else None
    )

    items: list[dict[str, Any]] = []

    # 1. Bollinger breakout: a fresh close above the 20-period upper band.
    description = "收盘价由布林带内向上突破 20 周期上轨，确认波动区间向上扩张。"
    if count < 21:
        items.append(
            _insufficient("bollinger_breakout", "布林突破", "突破", description, 21, count)
        )
    else:
        _, middle, upper = indicators.bollinger(closes, 20, 2)
        _, _, previous_upper = indicators.bollinger(closes[:-1], 20, 2)
        triggered = bool(
            upper is not None
            and previous_upper is not None
            and current_close > upper
            and previous_close <= previous_upper
        )
        items.append(
            _item(
                "bollinger_breakout",
                "布林突破",
                "突破",
                description,
                triggered,
                f"收盘价 {_price(current_close)}，上轨 {_price(upper)}，{'已形成最新突破' if triggered else '尚未形成由内向外的有效突破'}。",
                [
                    _metric("收盘价", _price(current_close)),
                    _metric("中轨 MA20", _price(middle)),
                    _metric("上轨", _price(upper)),
                    _metric("前一上轨", _price(previous_upper)),
                ],
            )
        )

    # Shared moving averages.
    ma10 = indicators.sma(closes, 10)
    ma20 = indicators.sma(closes, 20)
    ma50 = indicators.sma(closes, 50)
    ma60 = indicators.sma(closes, 60)

    # 2. MA pullback bounce.
    description = "MA20 位于 MA50 上方，价格回踩 MA20 后收回均线之上并收阳。"
    if count < 50:
        items.append(
            _insufficient(
                "moving_average_pullback_bounce", "均线回踩反弹", "趋势", description, 50, count
            )
        )
    else:
        distance = (current_close / ma20 - 1) * 100 if ma20 else None
        touched = bool(ma20 and current_low <= ma20 * 1.01)
        triggered = bool(
            ma20
            and ma50
            and ma20 > ma50
            and touched
            and current_close > ma20
            and current_close >= current_open
        )
        items.append(
            _item(
                "moving_average_pullback_bounce",
                "均线回踩反弹",
                "趋势",
                description,
                triggered,
                f"MA20 {_price(ma20)} / MA50 {_price(ma50)}，最新最低价 {_price(current_low)}；{'回踩后重新站上 MA20' if triggered else '尚未同时满足趋势、回踩和收复条件'}。",
                [
                    _metric("收盘价", _price(current_close)),
                    _metric("MA20", _price(ma20)),
                    _metric("MA50", _price(ma50)),
                    _metric("偏离 MA20", _percent(distance)),
                ],
            )
        )

    # 3. Structural trend breakout with volume confirmation.
    description = "收盘价突破此前 20 根 K 线最高价，并由至少 1.2 倍均量确认。"
    if count < 21:
        items.append(_insufficient("trend_breakout", "趋势突破", "突破", description, 21, count))
    else:
        prior_high = max(highs[-21:-1])
        triggered = bool(
            current_close > prior_high and volume_ratio is not None and volume_ratio >= 1.2
        )
        items.append(
            _item(
                "trend_breakout",
                "趋势突破",
                "突破",
                description,
                triggered,
                f"收盘价 {_price(current_close)}，前 20 期高点 {_price(prior_high)}，量比 {_ratio(volume_ratio)}；{'突破并获得量能确认' if triggered else '价格突破与量能确认尚未同时成立'}。",
                [
                    _metric("收盘价", _price(current_close)),
                    _metric("前高", _price(prior_high)),
                    _metric("量比", _ratio(volume_ratio)),
                    _metric(
                        "突破幅度",
                        _percent((current_close / prior_high - 1) * 100 if prior_high else None),
                    ),
                ],
            )
        )

    # 4. Price and volume rise together.
    description = "最新收盘价上涨，近 3 期累计为正，且成交量达到 20 期均量的 1.3 倍。"
    if count < 21:
        items.append(_insufficient("price_volume_rise", "量价齐升", "动量", description, 21, count))
    else:
        three_bar_return = (
            (current_close / closes[-4] - 1) * 100 if count >= 4 and closes[-4] else None
        )
        triggered = bool(
            previous_close is not None
            and current_close > previous_close
            and (three_bar_return or 0) > 0
            and volume_ratio is not None
            and volume_ratio >= 1.3
        )
        items.append(
            _item(
                "price_volume_rise",
                "量价齐升",
                "动量",
                description,
                triggered,
                f"单期涨跌 {_percent((current_close / previous_close - 1) * 100 if previous_close else None)}，3 期涨跌 {_percent(three_bar_return)}，量比 {_ratio(volume_ratio)}；{'价格与成交量同步走强' if triggered else '量价同步条件尚未全部满足'}。",
                [
                    _metric("最新收盘", _price(current_close)),
                    _metric(
                        "单期涨跌",
                        _percent(
                            (current_close / previous_close - 1) * 100 if previous_close else None
                        ),
                    ),
                    _metric("3 期涨跌", _percent(three_bar_return)),
                    _metric("量比", _ratio(volume_ratio)),
                ],
            )
        )

    # 5. New-low reversal.
    description = "盘中创 20 期新低后收复此前低点，并以阳线高于前收确认反转。"
    if count < 21:
        items.append(_insufficient("new_low_reversal", "新低反转", "反转", description, 21, count))
    else:
        prior_low = min(lows[-21:-1])
        triggered = bool(
            current_low < prior_low
            and current_close > prior_low
            and previous_close is not None
            and current_close > previous_close
            and current_close > current_open
        )
        items.append(
            _item(
                "new_low_reversal",
                "新低反转",
                "反转",
                description,
                triggered,
                f"最低价 {_price(current_low)}，前 20 期低点 {_price(prior_low)}，收盘 {_price(current_close)}；{'创新低后完成收复' if triggered else '尚未形成创新低后的有效收复'}。",
                [
                    _metric("最低价", _price(current_low)),
                    _metric("前低", _price(prior_low)),
                    _metric("收盘价", _price(current_close)),
                    _metric(
                        "K 线实体",
                        _percent(
                            (current_close / current_open - 1) * 100 if current_open else None
                        ),
                    ),
                ],
            )
        )

    # 6. Low-volume pullback in an established uptrend.
    description = "MA20 高于 MA50，价格在 MA20 附近止跌，成交量低于 20 期均量的 0.8 倍。"
    if count < 50:
        items.append(
            _insufficient("low_volume_pullback", "缩量回踩", "趋势", description, 50, count)
        )
    else:
        distance = (current_close / ma20 - 1) * 100 if ma20 else None
        near_ma20 = distance is not None and -1.0 <= distance <= 2.0
        triggered = bool(
            ma20
            and ma50
            and ma20 > ma50
            and near_ma20
            and volume_ratio is not None
            and volume_ratio <= 0.8
            and current_close >= current_open
        )
        items.append(
            _item(
                "low_volume_pullback",
                "缩量回踩",
                "趋势",
                description,
                triggered,
                f"价格偏离 MA20 {_percent(distance)}，量比 {_ratio(volume_ratio)}；{'上升趋势中的缩量回踩成立' if triggered else '趋势、位置、量能和止跌条件尚未同时成立'}。",
                [
                    _metric("收盘价", _price(current_close)),
                    _metric("MA20", _price(ma20)),
                    _metric("MA50", _price(ma50)),
                    _metric("量比", _ratio(volume_ratio)),
                ],
            )
        )

    # 7. Strong gap/open for the selected bar period (not an official US session gap).
    description = "当前周期开放价较上一周期收盘高开至少 2%，且当前收盘不低于开放价。"
    if count < 2:
        items.append(_insufficient("strong_gap_open", "强势高开", "突破", description, 2, count))
    else:
        gap = (current_open / previous_close - 1) * 100 if previous_close else None
        triggered = bool(gap is not None and gap >= 2 and current_close >= current_open)
        items.append(
            _item(
                "strong_gap_open",
                "强势高开",
                "突破",
                description,
                triggered,
                f"{timeframe} 周期开放价较前收 {_percent(gap)}，收盘较开放 {_percent((current_close / current_open - 1) * 100 if current_open else None)}；{'高开后保持强势' if triggered else '未达到 2% 高开并守住开放价'}。",
                [
                    _metric("前收", _price(previous_close)),
                    _metric("开放价", _price(current_open)),
                    _metric("高开幅度", _percent(gap)),
                    _metric("收盘价", _price(current_close)),
                ],
            )
        )

    # 8. Bullish MA alignment.
    description = "收盘价位于 MA10、MA20、MA50 上方，且三条均线按短到长依次抬高。"
    if count < 50:
        items.append(
            _insufficient("moving_average_bull", "均线多头", "趋势", description, 50, count)
        )
    else:
        triggered = bool(ma10 and ma20 and ma50 and current_close > ma10 > ma20 > ma50)
        items.append(
            _item(
                "moving_average_bull",
                "均线多头",
                "趋势",
                description,
                triggered,
                f"收盘 {_price(current_close)} / MA10 {_price(ma10)} / MA20 {_price(ma20)} / MA50 {_price(ma50)}；{'标准多头排列成立' if triggered else '尚未形成完整多头排列'}。",
                [
                    _metric("收盘价", _price(current_close)),
                    _metric("MA10", _price(ma10)),
                    _metric("MA20", _price(ma20)),
                    _metric("MA50", _price(ma50)),
                ],
            )
        )

    # 9. Fresh MA20/MA60 golden cross.
    description = "MA20 在最新一根 K 线上穿 MA60，识别新发生的金叉而非持续多头状态。"
    if count < 61:
        items.append(_insufficient("ma_golden_cross", "MA金叉", "趋势", description, 61, count))
    else:
        previous_ma20 = indicators.sma(closes[:-1], 20)
        previous_ma60 = indicators.sma(closes[:-1], 60)
        triggered = bool(
            previous_ma20 is not None
            and previous_ma60 is not None
            and ma20 is not None
            and ma60 is not None
            and previous_ma20 <= previous_ma60
            and ma20 > ma60
        )
        items.append(
            _item(
                "ma_golden_cross",
                "MA金叉",
                "趋势",
                description,
                triggered,
                f"当前 MA20 {_price(ma20)} / MA60 {_price(ma60)}，前一周期 MA20 {_price(previous_ma20)} / MA60 {_price(previous_ma60)}；{'最新金叉成立' if triggered else '本周期没有新发生金叉'}。",
                [
                    _metric("MA20", _price(ma20)),
                    _metric("MA60", _price(ma60)),
                    _metric("前 MA20", _price(previous_ma20)),
                    _metric("前 MA60", _price(previous_ma60)),
                ],
            )
        )

    # 10. Fresh MACD golden cross plus volume.
    description = "DIF 最新上穿 DEA，同时成交量达到 20 期均量的 1.2 倍。"
    if count < 36:
        items.append(
            _insufficient(
                "macd_golden_cross_volume", "MACD金叉放量", "动量", description, 36, count
            )
        )
    else:
        dif, dea, histogram = indicators.macd(closes)
        previous_dif, previous_dea, _ = indicators.macd(closes[:-1])
        crossed = bool(
            dif is not None
            and dea is not None
            and previous_dif is not None
            and previous_dea is not None
            and previous_dif <= previous_dea
            and dif > dea
        )
        triggered = bool(crossed and volume_ratio is not None and volume_ratio >= 1.2)
        items.append(
            _item(
                "macd_golden_cross_volume",
                "MACD金叉放量",
                "动量",
                description,
                triggered,
                f"DIF {_price(dif)} / DEA {_price(dea)} / 柱体 {_price(histogram)}，量比 {_ratio(volume_ratio)}；{'金叉与放量同时成立' if triggered else '金叉和放量尚未同时成立'}。",
                [
                    _metric("DIF", _price(dif)),
                    _metric("DEA", _price(dea)),
                    _metric("MACD 柱", _price(histogram)),
                    _metric("量比", _ratio(volume_ratio)),
                ],
            )
        )

    # 11. Oversold bounce.
    description = "RSI(14) 位于 25～40 的低位恢复区，且最新收盘高于前收并收阳。"
    if count < 16:
        items.append(_insufficient("oversold_bounce", "超跌反弹", "反转", description, 16, count))
    else:
        rsi14 = indicators.rsi(closes, 14)
        triggered = bool(
            rsi14 is not None
            and 25 < rsi14 <= 40
            and previous_close is not None
            and current_close > previous_close
            and current_close > current_open
        )
        items.append(
            _item(
                "oversold_bounce",
                "超跌反弹",
                "反转",
                description,
                triggered,
                f"RSI(14)={rsi14:.1f}，单期涨跌 {_percent((current_close / previous_close - 1) * 100 if previous_close else None)}；{'低位反弹条件成立' if triggered else 'RSI 位置与价格反弹尚未同时确认'}。",
                [
                    _metric("RSI(14)", f"{rsi14:.1f}"),
                    _metric("前收", _price(previous_close)),
                    _metric("收盘价", _price(current_close)),
                    _metric(
                        "K 线实体",
                        _percent(
                            (current_close / current_open - 1) * 100 if current_open else None
                        ),
                    ),
                ],
            )
        )

    # 12. Deep oversold reversal.
    description = "RSI(9) 从 20 以下重新上穿 20，并以阳线确认极端超卖后的方向反转。"
    if count < 11:
        items.append(_insufficient("oversold_reversal", "超跌反转", "反转", description, 11, count))
    else:
        rsi9 = indicators.rsi(closes, 9)
        previous_rsi9 = indicators.rsi(closes[:-1], 9)
        triggered = bool(
            previous_rsi9 is not None
            and rsi9 is not None
            and previous_rsi9 <= 20 < rsi9
            and current_close > current_open
        )
        items.append(
            _item(
                "oversold_reversal",
                "超跌反转",
                "反转",
                description,
                triggered,
                f"前 RSI(9)={previous_rsi9:.1f}，当前 RSI(9)={rsi9:.1f}；{'已离开极端超卖区并收阳' if triggered else '尚未完成从 20 以下向上的反转确认'}。",
                [
                    _metric("前 RSI(9)", f"{previous_rsi9:.1f}"),
                    _metric("当前 RSI(9)", f"{rsi9:.1f}"),
                    _metric("开放价", _price(current_open)),
                    _metric("收盘价", _price(current_close)),
                ],
            )
        )

    if len(items) != STRATEGY_INDICATOR_COUNT:  # pragma: no cover - invariant
        raise RuntimeError("strategy indicator count mismatch")
    return _result(timeframe, items, count, clean[-1]["open_time"])


def evaluate_directional_strategy_indicators(
    candles: Sequence[Mapping[str, Any]], timeframe: str
) -> dict[str, Any]:
    """Evaluate each strategy family for both long and short opportunities.

    Short conditions are evaluated on the reciprocal OHLC series. A price decline,
    lower-band break, bearish moving-average alignment, death cross, or overbought
    reversal therefore maps to the existing and tested bullish family condition
    without weakening its thresholds.
    """

    bullish = evaluate_strategy_indicators(candles, timeframe)
    bearish = evaluate_strategy_indicators(_reciprocal_candles(candles), timeframe)
    bearish_by_key = {item["key"]: item for item in bearish["items"]}
    for item in bullish["items"]:
        opposite = bearish_by_key[item["key"]]
        item["bullish_triggered"] = item["triggered"]
        item["bearish_triggered"] = opposite["triggered"]
        item["bearish_name"] = BEARISH_STRATEGY_NAMES[item["key"]]
        item["bearish_status"] = opposite["status"]
        item["bearish_summary"] = (
            f"{BEARISH_STRATEGY_NAMES[item['key']]}反向条件"
            f"{'已满足' if opposite['triggered'] is True else '尚未满足' if opposite['triggered'] is False else '数据不足'}。"
        )
        item["direction"] = (
            "both"
            if item["triggered"] is True and opposite["triggered"] is True
            else "bullish"
            if item["triggered"] is True
            else "bearish"
            if opposite["triggered"] is True
            else None
        )
    bullish["bullish_triggered_count"] = sum(
        item["bullish_triggered"] is True for item in bullish["items"]
    )
    bullish["bearish_triggered_count"] = sum(
        item["bearish_triggered"] is True for item in bullish["items"]
    )
    return bullish


def _reciprocal_candles(
    candles: Sequence[Mapping[str, Any]],
) -> list[dict[str, float | int]]:
    """Mirror positive OHLC prices so bullish tests become bearish tests."""

    result: list[dict[str, float | int]] = []
    for candle in candles:
        open_price = _number(candle["open"])
        high_price = _number(candle["high"])
        low_price = _number(candle["low"])
        close_price = _number(candle["close"])
        if min(open_price, high_price, low_price, close_price) <= 0:
            return []
        result.append(
            {
                "open_time": int(candle.get("open_time") or 0),
                "open": 1 / open_price,
                "high": 1 / low_price,
                "low": 1 / high_price,
                "close": 1 / close_price,
                "volume": _number(candle["volume"]),
            }
        )
    return result


def _result(
    timeframe: str,
    items: Sequence[dict[str, Any]],
    candle_count: int,
    evaluated_at: int | None,
) -> dict[str, Any]:
    return {
        "timeframe": timeframe,
        "candle_count": candle_count,
        "evaluated_at": evaluated_at,
        "count": len(items),
        "triggered_count": sum(item["triggered"] is True for item in items),
        "items": list(items),
    }


def _definitions() -> tuple[tuple[str, str, str, str, int], ...]:
    return (
        ("bollinger_breakout", "布林突破", "突破", "收盘价由布林带内向上突破 20 周期上轨。", 21),
        (
            "moving_average_pullback_bounce",
            "均线回踩反弹",
            "趋势",
            "上升趋势中回踩 MA20 后收复。",
            50,
        ),
        ("trend_breakout", "趋势突破", "突破", "突破 20 期结构高点并放量。", 21),
        ("price_volume_rise", "量价齐升", "动量", "价格上涨并获得成交量确认。", 21),
        ("new_low_reversal", "新低反转", "反转", "创新低后收复前低。", 21),
        ("low_volume_pullback", "缩量回踩", "趋势", "上升趋势中缩量回踩 MA20。", 50),
        ("strong_gap_open", "强势高开", "突破", "当前周期高开后保持强势。", 2),
        ("moving_average_bull", "均线多头", "趋势", "收盘与多条均线形成多头排列。", 50),
        ("ma_golden_cross", "MA金叉", "趋势", "MA20 最新上穿 MA60。", 61),
        ("macd_golden_cross_volume", "MACD金叉放量", "动量", "MACD 最新金叉并获得量能确认。", 36),
        ("oversold_bounce", "超跌反弹", "反转", "RSI 低位恢复并出现价格反弹。", 16),
        ("oversold_reversal", "超跌反转", "反转", "RSI 从极端超卖区上穿确认。", 11),
    )
