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
        'id="open-symbol-picker"',
        'id="symbol-picker-dialog" class="symbol-dialog-backdrop hidden"',
        'id="symbol-search" type="search"',
        'id="symbol-choice-list"',
        'id="confirm-symbol-picker"',
        'id="range-feedback"',
        "this.openSymbolPicker()",
        "this.confirmSymbolSelection()",
        "请至少选择 1 个交易品种后再确定",
        "超出历史库存，已自动使用最大可用范围",
        "首次回测将按需同步；完成后显示具体范围",
        'id="market-data-source" name="market_data_source"',
        '<option value="binance" selected>仅使用 Binance 合约行情</option>',
        "自动选择（Tiger 优先，失败转 Binance）",
        "Tiger 历史优先（不足转 Binance）",
        'market_data_source: this.q("#market-data-source").value',
        'sourceSelect.value = "binance"',
        "回测会自动向前补取指标预热数据",
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
        'class="calculator-summary-table" aria-label="多空止盈价格与收益计算"',
        '<tbody id="calculator-summary"></tbody>',
        'id="calculator-apply-status" class="calculator-apply-status" role="status" aria-live="polite"',
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
        'this.calculatorTargetRow(',
        '"做多"',
        '"涨幅"',
        '"做空"',
        '"跌幅"',
        'values.priceMovePct',
        'this.calculatorMoney(values.grossProfit, true)',
    ):
        assert contract in script

    assert 'id="calculator-target-roe"' not in script
    assert 'this.q("#lot-calculator-dialog").addEventListener("click"' not in script
    assert 'event.key === "Escape" && !this.q("#lot-calculator-dialog")' not in script
    apply_block = script.split("  applyCalculatedPoints(scope) {", 1)[1].split("\n  resolveBounds()", 1)[0]
    assert "this.closeLotCalculator();" not in apply_block
    assert "scrollIntoView" not in apply_block
    assert "点击右上角 × 关闭" in apply_block
    assert "const priceMove = basePoints * pointSize;" in script
    assert "const priceMovePct = priceMove / price * 100;" in script
    assert "const longTakeProfitPrice = price + priceMove;" in script
    assert "const shortTakeProfitPrice = priceMove < price ? price - priceMove : null;" in script
    assert "const grossProfit = priceMove * lot;" in script
    assert "const grossRoePct = grossProfit / positionMargin * 100;" in script
    assert "const estimatedNetRoePct = estimatedNetProfit / positionMargin * 100;" in script
    assert "renderLotCalculator(resetPointFields = false)" in script
    assert '`${this.price(values.longTakeProfitPrice)} USDT`' in script
    assert "shortTakeProfitLabel" in script
    assert 'const lot = Number(this.q("#calculator-lot").value);' in script
    assert 'const leverage = Number(this.q("#calculator-leverage").value);' in script
    assert 'if (scope === "all") this.applyCalculatorPositionSettings();' in script
    assert 'const takeProfitKeys = new Set(["TP", "TP2", "TP3", "TP4"]);' in script
    assert 'TrailStart: scaled(6)' in script

    assert 'id="standard-execution-note"' in script
    assert 'id="price-chart"' in script
    assert "drawPriceChart(" in script
    assert "滚轮缩放 · 拖拽平移 · 双击复位 · 悬停查看成交；收益仅在平仓点显示" in script
    assert 'id="price-chart-tooltip"' in script
    assert 'id="price-chart-range"' in script
    assert 'canvas.addEventListener("wheel"' in script
    assert 'canvas.addEventListener("pointerdown"' in script
    assert 'canvas.addEventListener("dblclick"' in script
    assert "this.renderPriceChartTooltip(event.offsetX, event.offsetY);" in script
    assert 'const executions = this.tradeExecutions(trade' in script
    assert 'executions.forEach((execution, index) =>' in script
    assert 'const markerBounds = []' in script
    assert 'context.measureText(label).width' in script
    assert 'layout.markers.push({ x, y, baseX, baseY, kind, trade, execution, isFinalExit, time: ts });' in script
    assert 'context.lineTo(x, y)' in script
    assert 'isBuy ? "买平" : "卖平"' in script
    assert 'action === "add" ? (isBuy ? "买加" : "卖加")' in script
    assert 'if (isExit) {' in script
    assert '[isFinalExit ? "本周期净盈亏" : "本次平仓净盈亏", this.signedMoney(pnl), tone]' in script
    assert 'if (isFinalExit) {' in script
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
    assert 'this.q("#open-symbol-picker").disabled = !this.symbolOptions.length;' in script
    assert 'new Intl.NumberFormat("zh-CN").format(bars)' in script
    assert 'start.min = min || "";' in script
    assert '<aside class="history-panel">' not in script
    assert 'id="refresh-history"' not in script


def test_backtest_exposes_calculation_pipeline_and_progressive_replay() -> None:
    script = (ROOT / "web/src/controllers/backtest.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "web/public/assets/backtest.css").read_text(encoding="utf-8")

    for contract in (
        'id="running-result" class="running-result hidden"',
        'data-running-step="0"',
        'data-running-step="4"',
        "服务端正在逐根执行策略、撮合订单并计算账户权益。",
        "收到结果后，将按真实历史时间轴逐根回放。",
        "await this.replayResult(detail, generation);",
        "drawReplayFrame(visibleCount)",
        "state.candles.slice(0, visibleCount)",
        "正在回放 ${this.integer(visibleCount)} / ${this.integer(state.candles.length)} 根 K 线",
        'id="trade-cycle-rail" class="trade-cycle-rail hidden"',
        'id="trade-replay-status" class="trade-replay-status hidden"',
    ):
        assert contract in script

    assert ".stage-layout.has-result" in stylesheet
    assert "grid-template-columns: minmax(0, 1fr) minmax(320px, 372px)" in stylesheet
    assert ".trade-cycle-rail {" in stylesheet
    assert "position: sticky" in stylesheet
    assert "container-type: inline-size" in stylesheet
    assert "@container (max-width: 960px)" in stylesheet


def test_backtest_workspace_uses_adaptive_page_width() -> None:
    controller = (ROOT / "web/src/controllers/backtest.js").read_text(encoding="utf-8")
    app = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")
    stylesheet = (ROOT / "web/src/styles.css").read_text(encoding="utf-8")

    assert "/next/assets/backtest.css?v=20260904-symbol-dialog-1" in controller
    assert 'page === "backtest" ? " backtest-mode"' in app
    assert ".workspace-content.backtest-mode" in stylesheet
    assert "calc(100% - clamp(16px, 1.25vw, 32px))" in stylesheet


def test_backtest_supports_parameter_profiles_and_multiple_symbols() -> None:
    script = (ROOT / "web/src/controllers/backtest.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "web/public/assets/backtest.css").read_text(encoding="utf-8")

    for contract in (
        'id="selected-symbols"',
        'id="open-symbol-picker"',
        'aria-controls="symbol-picker-dialog"',
        'role="listbox" aria-multiselectable="true"',
        'this.draftSymbols = [...this.selectedSymbols]',
        'this.selectedSymbols = this.draftSymbols.filter',
        'id="save-default-profile"',
        'id="save-symbol-profile"',
        'id="profile-status"',
        'saveParameterProfile("default")',
        'saveParameterProfile("symbol")',
        "symbolsForRun()",
        "primarySymbol()",
        "const concurrency = Math.max(1, Math.min(2",
        "await Promise.all(Array.from({ length: concurrency }, () => runNext()))",
        "多品种回测完成",
        "专有交易策略参数已保存",
        "/strategy-parameters/",
    ):
        assert contract in script

    assert ".backtest-action-row" in stylesheet
    assert ".selected-symbols" in stylesheet
    assert ".symbol-chip" in stylesheet
    assert ".symbol-dialog-backdrop" in stylesheet
    assert ".symbol-choice-list" in stylesheet
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in stylesheet


def test_backtest_configuration_uses_wide_dialog() -> None:
    script = (ROOT / "web/src/controllers/backtest.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "web/public/assets/backtest.css").read_text(encoding="utf-8")

    for contract in (
        'id="open-backtest-config"',
        'id="backtest-config-dialog" class="config-dialog-backdrop hidden"',
        'id="backtest-form" class="config-dialog" role="dialog"',
        'id="close-backtest-config"',
        "配置并开始回测",
        "openConfigDialog()",
        "closeConfigDialog(false)",
        'event.key === "Escape"',
        'setAttribute("aria-expanded", "true")',
    ):
        assert contract in script

    assert ".config-dialog-backdrop" in stylesheet
    assert "width: min(1320px, 100%)" in stylesheet
    assert ".config-form-grid" in stylesheet
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in stylesheet
