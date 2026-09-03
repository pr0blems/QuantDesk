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
        'id="data-availability"',
        'id="available-range"',
        'id="available-bars"',
        'type="search" list="symbol-options"',
        'id="symbol-options"',
        "this.handleSymbolSearch()",
        "请输入并选择列表中的有效交易品种",
        "首次回测将按需同步；完成后显示具体范围",
    ):
        assert contract in script

    assert 'id="standard-execution-note"' in script
    assert 'id="price-chart"' in script
    assert "drawPriceChart(" in script
    assert 'id="leverage" name="leverage" required' in script
    assert '<option value="20">20x</option>' in script
    assert 'margin_mode: "isolated"' in script
    assert 'id="position-field"' in script
    assert 'id="stop-field"' in script
    assert 'input.step = param.step != null ? String(param.step) : "any";' in script
    assert "this.syncBounds(basket && changed);" in script
    assert "this.shiftMonths(end.value, -1)" in script
    assert "this.renderAvailability(bounds);" in script
    assert "this.populateSymbolSearch(symbols" in script
    assert 'input.disabled = !this.symbolOptions.length;' in script
    assert 'new Intl.NumberFormat("zh-CN").format(bars)' in script
    assert 'start.min = min || "";' in script
