from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_prediction_analytics_renders_frozen_ablation_without_imputation() -> None:
    component = (
        ROOT / "web/src/controllers/ai-monitor.js"
    ).read_text(encoding="utf-8")
    stylesheet = (
        ROOT / "web/public/assets/ai-monitor.css"
    ).read_text(encoding="utf-8")

    assert 'id="market-ablation"' in component
    assert "this.renderMarketAblation(data.ablation)" in component
    for key in ("baseline", "quote_halt", "option_flow", "full"):
        assert f'"{key}"' in component
    for label in (
        "市场数据模块消融对比",
        "可用样本",
        "命中率",
        "净期望",
        "Profit Factor",
        "最大回撤",
        "信号保留",
    ):
        assert label in component
    assert 'variant.status === "unavailable"' in component
    assert "系统不会用当前行情、0 分或中性值补齐" in component
    assert 'ablation.causal_replay === true' in component
    assert "不是反事实因果回放" in component
    for selector in (
        ".market-ablation",
        ".ablation-variants",
        ".ablation-variant.unavailable",
        ".ablation-coverage",
    ):
        assert selector in stylesheet


def test_prediction_advanced_filters_round_trip_through_url_query() -> None:
    component = (
        ROOT / "web/src/controllers/ai-monitor.js"
    ).read_text(encoding="utf-8")

    for control_id in (
        "prediction-date-from",
        "prediction-date-to",
        "prediction-symbol",
        "prediction-data-coverage-min",
        "prediction-feature-version",
        "prediction-decision-version",
        "prediction-direction",
        "prediction-news-score-min",
        "prediction-indicator-score-min",
    ):
        assert f'id="{control_id}"' in component
    for query_key in (
        "date_from",
        "date_to",
        "symbol",
        "direction",
        "news_score_min",
        "indicator_score_min",
        "min_data_coverage",
        "feature_version",
        "decision_version",
    ):
        assert f'"{query_key}"' in component
    assert "restorePredictionFiltersFromUrl()" in component
    assert "syncPredictionFiltersToUrl()" in component
    assert "window.history.replaceState" in component
    assert "new URLSearchParams(window.location.search)" in component
    assert 'params.set("min_data_coverage", String(filters.dataCoverageMin))' in component
    assert (
        "timezone_offset_minutes: String(-new Date().getTimezoneOffset())"
        in component
    )
    assert "信号开始日期不能晚于结束日期" in component
    assert 'if (dateFrom && !dateTo) dateTo = dateFrom;' in component
    assert 'if (!dateFrom && dateTo) dateFrom = dateTo;' in component
    assert 'let dateFrom = cleanDate(params.get("date_from"));' in component
    assert 'let dateTo = cleanDate(params.get("date_to"));' in component
    assert 'include_readiness: "false"' in component
    assert 'applyButton.textContent = "正在筛选…"' in component
    assert "applyButton.disabled = true" not in component
    assert 'this.api("/opportunity-readiness")' in component
    assert "cacheAge < 300000" in component
    assert "this.predictionAnalyticsAbortController?.abort()" in component
    assert "{ signal: controller.signal }" in component
    assert "const currentAnalytics = this.state.opportunityAnalytics || {};" in component
    assert "data.readiness || currentAnalytics.readiness || previousAnalytics.readiness" in component
    assert "if (!background)" in component
