from __future__ import annotations

import copy

import pytest

from quantdesk_v2.strategy_runtime import (
    TREND_PULLBACK_SPEC_V1,
    StrategyMarketDataError,
    StrategySpecError,
    adx_series,
    atr_series,
    build_trend_pullback_spec,
    ema_series,
    evaluate_strategy,
    strategy_spec_hash,
    validate_strategy_spec,
)


def _bars(count: int, *, start: float, step: float, volume: float = 100.0) -> list[dict]:
    rows = []
    price = start
    for index in range(count):
        close = price + step
        rows.append(
            {
                "open_time": index * 900,
                "open": price,
                "high": max(price, close) + 0.4,
                "low": min(price, close) - 0.4,
                "close": close,
                "volume": volume,
            }
        )
        price = close
    return rows


def _long_market() -> dict[str, list[dict]]:
    regime = _bars(90, start=100, step=0.8)
    setup = _bars(55, start=135, step=0.35)
    # A pullback into the setup EMA followed by recovery within the lookback.
    setup[-3]["low"] = setup[-3]["close"] - 8
    trigger = _bars(45, start=148, step=0.08)
    prior_high = max(item["high"] for item in trigger[-21:-1])
    trigger[-1]["close"] = prior_high + 1
    trigger[-1]["high"] = trigger[-1]["close"] + 0.4
    trigger[-1]["volume"] = 250
    return {"4h": regime, "1h": setup, "15m": trigger}


def test_indicator_kernel_has_deterministic_warmup() -> None:
    values = [float(index) for index in range(1, 41)]
    ema = ema_series(values, 10)
    assert ema[:9] == [None] * 9
    assert ema[9] == pytest.approx(5.5)

    highs = [item + 1 for item in values]
    lows = [item - 1 for item in values]
    atr = atr_series(highs, lows, values, 14)
    adx, plus_di, minus_di = adx_series(highs, lows, values, 14)
    assert atr[13] is None
    assert atr[14] == pytest.approx(2.0)
    assert adx[27] == pytest.approx(100.0)
    assert plus_di[-1] > minus_di[-1]


def test_full_strategy_spec_is_strict_and_hashable() -> None:
    validated = build_trend_pullback_spec()
    assert validated["strategy_type"] == "trend_pullback_continuation"
    assert strategy_spec_hash(validated) == strategy_spec_hash(copy.deepcopy(validated))

    invalid = copy.deepcopy(TREND_PULLBACK_SPEC_V1)
    invalid["parameters"]["regime_fast_ema"] = 80
    with pytest.raises(StrategySpecError, match="快速 EMA"):
        validate_strategy_spec(invalid)

    invalid = copy.deepcopy(TREND_PULLBACK_SPEC_V1)
    invalid["execution"]["closed_bar_only"] = False
    with pytest.raises(StrategySpecError, match="已收盘"):
        validate_strategy_spec(invalid)


def test_trend_pullback_strategy_emits_explainable_long_signal() -> None:
    decision = evaluate_strategy(TREND_PULLBACK_SPEC_V1, _long_market())
    assert decision.decision == "LONG_ENTRY"
    assert decision.signal_time is not None
    assert decision.confidence is not None and decision.confidence >= 0.6
    assert "REGIME_UP" in decision.reason_codes
    assert decision.evidence["setup"]["long_pullback"] is True
    assert decision.risk_proposal["stop_distance"] > 0


def test_signal_validity_preserves_millisecond_timestamp_units() -> None:
    market = _long_market()
    epoch_ms = 1_700_000_000_000
    for rows in market.values():
        for row in rows:
            row["open_time"] = epoch_ms + row["open_time"] * 1_000

    decision = evaluate_strategy(TREND_PULLBACK_SPEC_V1, market)

    assert decision.signal_time is not None
    assert decision.valid_until is not None
    assert decision.valid_until - decision.signal_time == 2 * 15 * 60 * 1_000


def test_strategy_skips_insufficient_or_invalid_market_data() -> None:
    market = _long_market()
    market["4h"] = market["4h"][:10]
    decision = evaluate_strategy(TREND_PULLBACK_SPEC_V1, market)
    assert decision.decision == "SKIP"
    assert decision.reason_codes == ("INSUFFICIENT_DATA",)

    invalid = _long_market()
    invalid["15m"][-1]["close"] = -1
    with pytest.raises(StrategyMarketDataError, match="价格无效"):
        evaluate_strategy(TREND_PULLBACK_SPEC_V1, invalid)
