"""Pure-Python indicators: SMA, EMA, RSI, MACD, ATR, Bollinger, OBV and SuperTrend."""

from __future__ import annotations

from collections.abc import Sequence


def sma(vals: Sequence[float], n: int) -> float | None:
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def sma_series(vals: Sequence[float], n: int) -> list[float | None]:
    output = []
    for index in range(len(vals)):
        if index + 1 < n:
            output.append(None)
        else:
            output.append(sum(vals[index + 1 - n : index + 1]) / n)
    return output


def ema_series(vals: Sequence[float], n: int) -> list[float]:
    if not vals:
        return []
    multiplier = 2 / (n + 1)
    output = [vals[0]]
    for value in vals[1:]:
        output.append(value * multiplier + output[-1] * (1 - multiplier))
    return output


def rsi(closes: Sequence[float], n: int = 14) -> float | None:
    if len(closes) < n + 1:
        return None
    gains = []
    losses = []
    for index in range(-n, 0):
        difference = closes[index] - closes[index - 1]
        gains.append(max(difference, 0))
        losses.append(max(-difference, 0))
    average_gain = sum(gains) / n
    average_loss = sum(losses) / n
    if average_loss == 0:
        return 100.0
    return 100 - 100 / (1 + average_gain / average_loss)


def macd(
    closes: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float | None, float | None, float | None]:
    """Return ``(DIF, DEA, histogram)`` or three ``None`` values if insufficient."""

    if len(closes) < slow + signal:
        return None, None, None
    fast_ema = ema_series(closes, fast)
    slow_ema = ema_series(closes, slow)
    dif_series = [
        fast_value - slow_value for fast_value, slow_value in zip(fast_ema, slow_ema, strict=True)
    ]
    dea_series = ema_series(dif_series[slow - 1 :], signal)
    dif = dif_series[-1]
    dea = dea_series[-1]
    return dif, dea, (dif - dea) * 2


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    n: int = 14,
) -> float | None:
    if len(closes) < n + 1:
        return None
    true_ranges = []
    for index in range(-n, 0):
        true_ranges.append(
            max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            )
        )
    return sum(true_ranges) / n


def bollinger(
    closes: Sequence[float], n: int = 20, mult: float = 2.0
) -> tuple[float | None, float | None, float | None]:
    if len(closes) < n:
        return None, None, None
    segment = closes[-n:]
    middle = sum(segment) / n
    variance = sum((value - middle) ** 2 for value in segment) / n
    standard_deviation = variance**0.5
    return (
        middle - mult * standard_deviation,
        middle,
        middle + mult * standard_deviation,
    )


def obv(closes: Sequence[float], volumes: Sequence[float]) -> list[float]:
    output = [0.0]
    for index in range(1, len(closes)):
        if closes[index] > closes[index - 1]:
            output.append(output[-1] + volumes[index])
        elif closes[index] < closes[index - 1]:
            output.append(output[-1] - volumes[index])
        else:
            output.append(output[-1])
    return output


def supertrend(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    n: int = 10,
    mult: float = 3.0,
) -> int | None:
    """Return the current standard SuperTrend direction: long ``1`` or short ``-1``."""

    length = len(closes)
    if length < n + 2:
        return None
    atrs: list[float | None] = [None] * length
    true_range_sum = 0.0
    for index in range(1, length):
        true_range = max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
        if index <= n:
            true_range_sum += true_range
            if index == n:
                atrs[index] = true_range_sum / n
        else:
            previous_atr = atrs[index - 1]
            if previous_atr is None:  # Defensive; the Wilder seed is set at ``n``.
                return None
            atrs[index] = (previous_atr * (n - 1) + true_range) / n

    final_upper = None
    final_lower = None
    direction = 1
    for index in range(n, length):
        current_atr = atrs[index]
        if current_atr is None:
            continue
        middle = (highs[index] + lows[index]) / 2
        basic_upper = middle + mult * current_atr
        basic_lower = middle - mult * current_atr
        if final_upper is None or final_lower is None:
            final_upper = basic_upper
            final_lower = basic_lower
            direction = 1 if closes[index] > middle else -1
            continue
        final_upper = (
            basic_upper
            if basic_upper < final_upper or closes[index - 1] > final_upper
            else final_upper
        )
        final_lower = (
            basic_lower
            if basic_lower > final_lower or closes[index - 1] < final_lower
            else final_lower
        )
        if direction == 1 and closes[index] < final_lower:
            direction = -1
        elif direction == -1 and closes[index] > final_upper:
            direction = 1
    return direction
