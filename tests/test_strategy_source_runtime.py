from __future__ import annotations

import pytest

from quantdesk_v2.strategy_source_runtime import (
    StrategySourceError,
    StrategySourceExecutionError,
    default_python_parameters,
    default_python_source,
    evaluate_source,
    source_hash,
    validate_source,
)


def _bars(count: int = 80) -> list[dict[str, float | int]]:
    result = []
    for index in range(count):
        price = 100 + index * 0.1
        result.append(
            {
                "open_time": 1_700_000_000 + index * 3_600,
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 1_000,
            }
        )
    return result


def test_default_python_source_is_real_executable_strategy_code() -> None:
    source = default_python_source()
    metadata = validate_source(source)
    _, params = default_python_parameters()

    decision = evaluate_source(
        source,
        {"symbol": "BTCUSDT", "decision_time": 1_700_400_000, "bars": {"1h": _bars()}},
        params,
    )

    assert metadata.language == "python"
    assert metadata.trigger_timeframe == "1h"
    assert metadata.source_hash == source_hash(source)
    assert decision.decision == "HOLD"
    assert decision.signal_time == _bars()[-1]["open_time"]
    assert decision.evidence["atr"] == pytest.approx(2.0)


def test_python_source_runtime_exposes_adx_as_a_pure_helper() -> None:
    source = '''TIMEFRAMES = ("1h",)
TRIGGER_TIMEFRAME = "1h"
LOOKBACK_BARS = 40
DIRECTIONS = ("long",)
def evaluate(context, params):
    bars = context["bars"][TRIGGER_TIMEFRAME]
    adx_value, plus_di, minus_di = adx(bars, int(params["adx_period"]))
    decision = "LONG_ENTRY" if adx_value > 20 and plus_di > minus_di else "HOLD"
    return {"decision": decision, "evidence": {"adx": adx_value, "plus_di": plus_di, "minus_di": minus_di}}
'''

    decision = evaluate_source(
        source,
        {"symbol": "BTCUSDT", "decision_time": 1_700_400_000, "bars": {"1h": _bars()}},
        {"adx_period": 14},
    )

    assert decision.decision == "LONG_ENTRY"
    assert decision.evidence["adx"] > 20
    assert decision.evidence["plus_di"] > decision.evidence["minus_di"]


@pytest.mark.parametrize(
    "unsafe_line",
    [
        "import os",
        "value = context.__class__",
        "open('strategy.txt')",
    ],
)
def test_source_validator_rejects_server_capability_access(unsafe_line: str) -> None:
    source = f'''TIMEFRAMES = ("1h",)
TRIGGER_TIMEFRAME = "1h"
LOOKBACK_BARS = 20
DIRECTIONS = ("long",)
def evaluate(context, params):
    {unsafe_line}
    return {{"decision": "HOLD"}}
'''

    with pytest.raises(StrategySourceError):
        validate_source(source)


def test_source_worker_times_out_infinite_strategy() -> None:
    source = '''TIMEFRAMES = ("1h",)
TRIGGER_TIMEFRAME = "1h"
LOOKBACK_BARS = 20
DIRECTIONS = ("long",)
def evaluate(context, params):
    while True:
        pass
'''

    with pytest.raises(StrategySourceExecutionError, match="超时"):
        evaluate_source(
            source,
            {"symbol": "BTCUSDT", "decision_time": 1, "bars": {"1h": _bars(20)}},
            {},
            timeout_seconds=0.2,
        )


def test_source_worker_rejects_invalid_decision_contract() -> None:
    source = '''TIMEFRAMES = ("1h",)
TRIGGER_TIMEFRAME = "1h"
LOOKBACK_BARS = 20
DIRECTIONS = ("long",)
def evaluate(context, params):
    return {"decision": "BUY_NOW"}
'''

    with pytest.raises(StrategySourceExecutionError, match="decision"):
        evaluate_source(
            source,
            {"symbol": "BTCUSDT", "decision_time": 1, "bars": {"1h": _bars(20)}},
            {},
        )
