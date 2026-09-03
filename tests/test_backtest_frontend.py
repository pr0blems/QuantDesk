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
        'this.node("button", "lot-calculator-trigger", "计算器")',
        'id="lot-calculator-dialog" class="calculator-dialog-backdrop hidden"',
        'this.q("#close-lot-calculator").addEventListener("click", () => this.closeLotCalculator());',
        'id="calculator-lot"',
        'id="calculator-leverage"',
        'id="calculator-market-change" class="neutral"',
        'id="calculator-summary" class="calculator-summary"',
        'data-calculator-param-key="TP"',
        '/position-calculator?symbol=',
        'this.applyCalculatedPoints("position")',
        'this.applyCalculatedPoints("take-profit")',
        'this.applyCalculatedPoints("all")',
        '一键应用全部设置',
        '按止盈点数、当前合约价格、初始手数和 Binance 杠杆估算实际收益',
        '基础止盈 TP',
        '预计净 ROE',
        'Binance 24h 涨跌幅',
        'quote.price_change_percent_24h',
        'rawMarketChangePercent != null',
        'this.calculatorMoney(values.grossProfit, true)',
    ):
        assert contract in script

    assert 'id="calculator-target-roe"' not in script
    assert 'this.q("#lot-calculator-dialog").addEventListener("click"' not in script
    assert 'event.key === "Escape" && !this.q("#lot-calculator-dialog")' not in script
    assert "const priceMove = basePoints * pointSize;" in script
    assert "const priceMovePct = priceMove / price * 100;" in script
    assert "const longTakeProfitPrice = price + priceMove;" in script
    assert "const shortTakeProfitPrice = priceMove < price ? price - priceMove : null;" in script
    assert "const grossProfit = priceMove * lot;" in script
    assert "const grossRoePct = grossProfit / positionMargin * 100;" in script
    assert "const estimatedNetRoePct = estimatedNetProfit / positionMargin * 100;" in script
    assert "renderLotCalculator(resetPointFields = false)" in script
    assert "做多止盈价" in script
    assert "做空止盈价" in script
    assert 'const lot = Number(this.q("#calculator-lot").value);' in script
    assert 'const leverage = Number(this.q("#calculator-leverage").value);' in script
    assert 'if (scope === "all") this.applyCalculatorPositionSettings();' in script
    assert 'const takeProfitKeys = new Set(["TP", "TP2", "TP3", "TP4"]);' in script
    assert 'TrailStart: scaled(6)' in script

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
    assert 'this.node("strong", "", `交易周期 #${this.integer(sequence)}`)' in script
    assert 'exitTime ? "已平仓" : "持仓中"' in script
    assert '"买卖执行明细"' in script
    assert 'orderSide === "buy" ? "买入时间" : "卖出时间"' in script
    assert 'return positionSide === "short" ? "卖出开空" : "买入开多";' in script
    assert 'return orderSide === "buy" ? "买入平空" : "卖出平多";' in script
    assert '["开仓保证金", this.money(initialMargin), ""]' in script
    assert '["杠杆倍率", `${this.integer(trade.leverage ?? 1)}x`, ""]' in script
    assert '["最低剩余可用", this.money(availableBalance)' in script
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
