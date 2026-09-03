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
        'id="range-feedback"',
        "this.handleSymbolSearch()",
        "请输入并选择列表中的有效交易品种",
        "超出历史库存，已自动使用最大可用范围",
        "首次回测将按需同步；完成后显示具体范围",
        'id="market-data-source" name="market_data_source"',
        "自动选择（Tiger 优先，失败转 Binance）",
        "Tiger 历史优先（不足转 Binance）",
        'market_data_source: this.q("#market-data-source").value',
        'id="open-history" type="button">历史回测数据</button>',
        'id="history-dialog" class="history-dialog-backdrop hidden"',
        'id="history-list" class="history-list"',
        "this.closeHistory();",
        '["实际数据源", sourceLabel]',
    ):
        assert contract in script

    assert 'id="standard-execution-note"' in script
    assert 'id="price-chart"' in script
    assert "drawPriceChart(" in script
    assert "滚轮缩放 · 拖拽平移 · 双击复位 · 悬停查看成交盈亏" in script
    assert 'id="price-chart-tooltip"' in script
    assert 'id="price-chart-range"' in script
    assert 'canvas.addEventListener("wheel"' in script
    assert 'canvas.addEventListener("pointerdown"' in script
    assert 'canvas.addEventListener("dblclick"' in script
    assert "this.renderPriceChartTooltip(event.offsetX, event.offsetY);" in script
    assert '["净盈亏", this.signedMoney(pnl), tone]' in script
    assert 'trade.account_return_pct ?? trade.return_pct ?? trade.pnl_pct' in script
    assert 'trade.margin_return_pct ?? trade.return_pct ?? trade.pnl_pct' in script
    assert '"开仓保证金", "杠杆", "剩余可用金额"' in script
    assert 'String(trade.position_structure ?? "").toLowerCase() === "mixed_basket"' in script
    assert 'isBasket ? "双向篮子"' in script
    assert 'id="leverage" name="leverage" required' in script
    assert '<option value="20">20x</option>' in script
    assert 'margin_mode: "isolated"' in script
    assert 'id="position-field"' in script
    assert 'id="stop-field"' in script
    assert 'input.step = param.step != null ? String(param.step) : "any";' in script
    assert "this.syncBounds(basket && changed);" in script
    assert "this.shiftMonths(end.value, -1)" in script
    assert 'const requestedStart = months === "all" ? (min || this.shiftMonths(end, -12))' in script
    assert 'button.setAttribute("aria-pressed", String(active));' in script
    assert "this.renderAvailability(bounds);" in script
    assert "this.populateSymbolSearch(symbols" in script
    assert 'input.disabled = !this.symbolOptions.length;' in script
    assert 'new Intl.NumberFormat("zh-CN").format(bars)' in script
    assert 'start.min = min || "";' in script
    assert '<aside class="history-panel">' not in script
    assert 'id="refresh-history"' not in script
