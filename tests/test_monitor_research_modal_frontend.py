from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_monitor_research_modal_keeps_existing_actions_and_adds_research_structure() -> None:
    script = (ROOT / "web" / "src" / "controllers" / "monitor.js").read_text(encoding="utf-8")

    for marker in (
        'class="modal-box research-modal"',
        'class="research-sidebar"',
        'class="research-workspace"',
        '<span>01</span><strong>趋势</strong><small>K线与成交量</small>',
        '<span>07</span><strong>新闻列表</strong><small>老虎证券资讯</small>',
        'id="modal-metric-price"',
        'id="modal-metric-volume"',
        'id="modal-metric-depth"',
        'id="modal-metric-battle"',
        'id="modal-metric-quality"',
        'data-modal-section="#modal-trend"',
        'data-modal-section="#modal-indicator-section"',
        'data-modal-page="overview"',
        'data-modal-page="battle"',
        'data-modal-page="strategy"',
        'data-modal-page="report"',
        'data-modal-page="factors"',
        'data-modal-page="news"',
        'data-research-page="overview"',
        'data-research-page="battle" hidden',
        'id="strategy-indicator-list"',
        'id="prediction-feature-list"',
        'id="prediction-feature-caption"',
        'id="strategy-indicator-detail"',
        'id="modal-ohlc"',
        'id="chart-stage"',
        'id="chart-tooltip"',
        'data-chart-overlay="ma60"',
        'data-chart-overlay="boll"',
        'data-chart-overlay="signals"',
        'data-chart-overlay="projection"',
        'data-chart-action="reset"',
        'id="chart-projection-note"',
        'id="battle-detail"',
        'id="opportunity-detail"',
        'id="score-summary"',
        'id="report"',
        'id="factors"',
        'data-modal-section="#modal-news-section"',
        'id="research-news-list"',
        'data-opportunity-action="shadow"',
    ):
        assert marker in script

    assert "renderModalSummary(overview, klines, report" in script
    assert "this.api(`/strategy-indicators?symbol=${encoded}&tf=${timeframe}`)" in script
    assert "this.api(`/klines?symbol=${encoded}&tf=${timeframe}&limit=300`)" in script
    assert "limit=1&include_ignored=true" in script
    assert "include_expired=true" not in script
    assert "renderStrategyIndicators(indicatorScan)" in script
    assert "this.api(`/tiger-news?symbol=${encoded}&limit=30`)" in script
    assert "renderResearchNews(researchNews)" in script
    assert 'activateResearchPage(button, { smooth: true })' in script
    assert 'panel.hidden = panel.dataset.researchPage !== page' in script
    assert 'item.setAttribute("aria-selected", selected ? "true" : "false")' in script
    assert 'this.activateResearchPage(this.q(\'[data-modal-section="#modal-trend"]\'))' in script
    assert "老虎证券三路新闻接口聚合" in script
    assert "featureScan.bullish_count" in script
    assert 'bullish: "偏多"' in script
    assert 'bearish: "偏空"' in script
    assert "handleChartPointerMove(event)" in script
    assert "handleChartWheel(event)" in script
    assert "buildChartSignals(realKlines, realSeries)" in script
    assert 'item.direction === "long" ? "buy"' not in script
    assert "opportunities.forEach" not in script
    assert 'add(index, "buy", "MA金叉"' in script
    assert 'add(index, "sell", "MA死叉"' in script
    assert 'add(index, "buy", "布林突破"' in script
    assert 'add(index, "sell", "布林跌破"' in script
    assert "MA20 上穿 MA60" in script
    assert "MA20 下穿 MA60" in script
    assert "MA20 上穿 MA50" not in script
    assert "图中买点是历史 MA 金叉/布林上破" in script
    assert "最新一根 K 线：" in script
    assert "订单池深度" in script
    assert "美股映射 USDT 合约" in script
    assert "市盈率" not in script
    assert "市净率" not in script
    assert 'async openResearch(symbol, timeframe = "1h", predictionContext = null)' in script
    assert "buildPredictionProjection(klines, context)" in script
    assert "renderPredictionProjection(projection)" in script
    assert "evaluatePredictionProjection(klines, candles, anchorPrice, context)" in script
    assert "pathAccuracy * 0.7 + directionAccuracy * 0.3" in script
    assert "推演准确率" in script
    assert "终点价格偏差" in script
    assert "targetReturnPct" in script
    assert "非真实行情" in script
    assert 'this.api(`/overview?symbol=${encoded}`)' in script
    assert "fallbackOverview" in script
    assert "实时总览暂不可用，当前显示机会快照" in script


def test_monitor_research_modal_is_responsive_and_supports_light_theme() -> None:
    stylesheet = (ROOT / "web" / "public" / "assets" / "monitor.css").read_text(
        encoding="utf-8"
    )

    assert ".research-modal { width: min(1420px, 98vw)" in stylesheet
    assert "grid-template-columns: 180px minmax(0, 1fr)" in stylesheet
    assert ".research-tabs button.on" in stylesheet
    assert "box-shadow: inset 3px 0 0 #9f8dff" in stylesheet
    assert '.research-workspace [data-research-page][hidden]' in stylesheet
    assert ".research-metrics { display: grid" in stylesheet
    assert ".research-modal .report { grid-template-columns: repeat(3" in stylesheet
    assert ".chart-stage { position: relative" in stylesheet
    assert ".chart-tooltip { width: 206px" in stylesheet
    assert ".chart-overlay-toolbar" in stylesheet
    assert ".research-news-card" in stylesheet
    assert ".research-news-tags .kind" in stylesheet
    assert ".chart-signal-note" in stylesheet
    assert ".legend-line.projection" in stylesheet
    assert ".chart-projection-note" in stylesheet
    assert ".tooltip-projection" in stylesheet
    assert ".chart-projection-note .projection-accuracy" in stylesheet
    assert "touch-action: none" in stylesheet
    assert "@media (max-width: 620px)" in stylesheet
    assert ':host-context(html[data-theme="light"]) .research-modal' in stylesheet
    assert ':host([research-only]) .monitor' in stylesheet
    assert ':host([research-only]) #modal' in stylesheet


def test_prediction_algorithm_includes_all_twelve_kline_strategy_weights() -> None:
    script = (ROOT / "web" / "src" / "controllers" / "monitor.js").read_text(encoding="utf-8")
    feature_keys = (
        "kline_bollinger_breakout",
        "kline_moving_average_pullback_bounce",
        "kline_trend_breakout",
        "kline_price_volume_rise",
        "kline_new_low_reversal",
        "kline_low_volume_pullback",
        "kline_strong_gap_open",
        "kline_moving_average_bull",
        "kline_ma_golden_cross",
        "kline_macd_golden_cross_volume",
        "kline_oversold_bounce",
        "kline_oversold_reversal",
    )

    assert "市场与微观因子 · 8 项" in script
    assert "K 线策略信号 · 12 项" in script
    assert "20 项按周期权重加总" in script
    for key in feature_keys:
        assert script.count(f'data-algorithm-weight="{key}"') == 3
    assert script.count("data-algorithm-enabled=") == 20
    assert 'id="prediction-algorithm-optimize"' in script
    assert 'this.api("/prediction-algorithm/optimize"' in script
    assert "expected_config_version: data.config_version" in script
    assert '"X-QuantDesk-Algorithm-Version"' in script
    assert "DeepSeek AI 优化结果" in script
    assert "自动保存为新版本" in script
    assert "result.algorithm.config" in script
    assert "saved_config_version" in script
    assert 'id="prediction-algorithm-ai-trace"' in script
    assert 'id="prediction-algorithm-ai-history"' in script
    assert 'id="prediction-ai-history-modal"' in script
    assert 'id="prediction-ai-history-body"' in script
    assert 'id="prediction-ai-trace-modal"' in script
    assert 'id="prediction-ai-raw-output"' in script
    assert 'id="prediction-ai-normalization"' in script
    assert 'id="prediction-ai-history-analysis"' in script
    assert 'this.api("/prediction-algorithm/ai-trace")' in script
    assert 'this.api("/prediction-algorithm/ai-history?limit=50")' in script
    assert "/prediction-algorithm/ai-history/${encodeURIComponent(auditId)}" in script
    assert 'if (!this.state.algorithm.data?.editable) return;' in script
    assert '#prediction-algorithm-ai-history").classList.toggle("hidden", !data.editable)' in script
    assert '#prediction-algorithm-ai-history").disabled = !data.editable' in script
    assert "if (data.editable) await this.loadPredictionAiTrace();" in script
    assert "DeepSeek 提示词与推理摘要" in script
    assert "查看历史分析记录" in script
    assert "AI 历史分析记录" in script
    assert "数据库版本历史分析（实际投喂）" in script
    assert "training_history_analysis" in script
    assert "database_history_analysis" in script
    assert "按源版本及审计截止时间从数据库回算" in script
    assert "原始隐藏思维链" in script
    assert "config.enabled_features" in script


def test_prediction_algorithm_optimizer_has_review_ui() -> None:
    stylesheet = (ROOT / "web" / "public" / "assets" / "monitor.css").read_text(
        encoding="utf-8"
    )

    for marker in (
        ".prediction-algorithm-head-actions",
        ".algorithm-optimization-grid",
        ".algorithm-optimization-card.accepted",
        "[data-algorithm-enabled].on",
        ".algorithm-feature-disabled",
        ".prediction-ai-trace-box",
        ".prediction-ai-trace-section pre",
        ".prediction-ai-history-analysis",
        ".prediction-ai-history-box",
        ".prediction-ai-history-table",
    ):
        assert marker in stylesheet


def test_prediction_history_exposes_versioned_algorithm_snapshot_action() -> None:
    script = (ROOT / "web" / "src" / "controllers" / "monitor.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "web" / "public" / "assets" / "monitor.css").read_text(
        encoding="utf-8"
    )

    for marker in (
        "<th>最大有利 / 不利</th><th>操作</th>",
        'id="prediction-config-modal"',
        'data-prediction-config="${this.escape(item.public_id)}"',
        '模型 v${item.model_version ?? "--"} · 特征 v${item.feature_schema_version ?? "--"}',
        "/prediction-history/${encodeURIComponent(predictionId)}/algorithm",
        "旧数据未保存完整算法配置",
        "完整快照：以下参数就是生成这条预测时实际使用的配置",
        "K 线策略信号 · 12 项",
    ):
        assert marker in script
    assert ".history-prediction-config" in stylesheet
    assert ".prediction-config-table" in stylesheet
