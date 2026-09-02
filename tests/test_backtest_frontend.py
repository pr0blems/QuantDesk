from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_backtest_workbench_switches_to_martingale_basket_profile() -> None:
    script = (ROOT / "web/src/controllers/backtest.js").read_text(encoding="utf-8")

    for contract in (
        'backtest_profile === "martingale_tp4"',
        'strategy?.engine_key === "martingale_tp4"',
        "strategy?.supported_symbols",
        "strategy?.supported_timeframes",
        'param?.key !== "BoxTimeFrameMinutes"',
        'id="basket-profile-note"',
        "Tiger 历史 K 线按需同步",
    ):
        assert contract in script

    assert 'id="standard-execution-note"' in script
    assert 'id="position-field"' in script
    assert 'id="stop-field"' in script
