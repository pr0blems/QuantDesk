from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_paper_adjustment_edits_bound_strategy_parameters() -> None:
    script = (ROOT / "web/src/controllers/paper.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "web/public/assets/paper.css").read_text(encoding="utf-8")

    for contract in (
        'href="/next/assets/paper.css?v=20260905-paper-box-signals"',
        "调整策略参数",
        'id="paper-adjust-strategy"',
        'id="paper-adjust-parameters"',
        "strategy.parameter_schema",
        'data-paper-param-key="',
        "/api/v2/backtests/strategy-parameters/",
        'scope: this.adjustSymbol ? "symbol" : "default"',
        "下一轮信号计算时使用",
    ):
        assert contract in script

    for removed_account_setting in (
        'id="paper-adjust-leverage"',
        'id="paper-adjust-max-positions"',
        'id="paper-adjust-position-size"',
        'id="paper-adjust-margin-cap"',
    ):
        assert removed_account_setting not in script

    assert ".strategy-parameter-groups" in stylesheet
    assert ".strategy-parameter-grid" in stylesheet


def test_paper_can_manage_symbols_and_render_m15_charts() -> None:
    script = (ROOT / "web/src/controllers/paper.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "web/public/assets/paper.css").read_text(encoding="utf-8")

    for contract in (
        'id="paper-add-symbol"',
        "添加交易品种",
        'id="paper-symbol-picker"',
        'id="paper-market-charts"',
        'tf=15m&limit=160',
        "drawMarketCandles",
        "quantdeskOpenPaperMarketSocket",
        'message.event === "markets"',
        'data-market-change title="今日涨跌价格（今日涨跌幅）"',
        "snapshot.price_change_24h",
        "snapshot.pct_24h",
        "signedPrice(changePrice)",
        'canvas.addEventListener("pointerdown"',
        'canvas.addEventListener("wheel"',
        'canvas.addEventListener("dblclick"',
        "Math.floor(timestamp / 900_000) * 900_000",
        "拖拽平移 · 滚轮缩放 · 双击回到最新",
        'data-market-strategy="',
        "策略参数配置",
        '?symbol=${encodeURIComponent(this.adjustSymbol)}',
        'scope: this.adjustSymbol ? "symbol" : "default"',
        '{ symbol: this.adjustSymbol }',
        "品种专属策略参数",
        "保存并加载 K 线",
        "/chart-overlay?symbol=",
        "normalizeMarketBoxLevels",
        "paperMarketTradeMarkers",
        "箱体上沿",
        "箱体下沿",
        "买点",
        "卖点",
        "平仓点",
        "/accounts/${encodeURIComponent(this.selectedAccountId)}/symbols",
    ):
        assert contract in script

    assert ".market-chart-grid" in stylesheet
    assert ".market-canvas" in stylesheet
    assert ".market-canvas.dragging" in stylesheet
    assert ".market-strategy-button" in stylesheet
    assert ".market-chart-legend" in stylesheet
    assert ".signal-close" in stylesheet
    assert 'data-stream-status="live"' in stylesheet
    assert ".symbol-picker" in stylesheet


def test_paper_symbol_api_and_engine_are_account_scoped() -> None:
    routes = (
        ROOT / "src/quantdesk_v2/interfaces/api/trading_accounts.py"
    ).read_text(encoding="utf-8")
    engine = (ROOT / "src/quantdesk_v2/paper_engine.py").read_text(encoding="utf-8")

    for contract in (
        '@router.get("/paper/symbols")',
        '@router.put("/paper/accounts/{account_id}/symbols")',
        '@router.get("/paper/accounts/{account_id}/chart-overlay")',
        "build_box_levels(",
        "_paper_effective_martingale_snapshot",
        '"paper.account.symbols.update"',
        'deployment.universe_override_json = {',
    ):
        assert contract in routes

    assert "universe = _paper_symbols(account)" in engine
