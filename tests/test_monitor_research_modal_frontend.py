from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_monitor_research_modal_keeps_existing_actions_and_adds_research_structure() -> None:
    script = (ROOT / "src" / "quantdesk_v2" / "static" / "monitor.js").read_text(encoding="utf-8")

    for marker in (
        'class="modal-box research-modal"',
        'id="modal-metric-price"',
        'id="modal-metric-volume"',
        'id="modal-metric-depth"',
        'id="modal-metric-battle"',
        'id="modal-metric-quality"',
        'data-modal-section="#modal-trend"',
        'data-modal-section="#modal-indicator-section"',
        'id="strategy-indicator-list"',
        'id="strategy-indicator-detail"',
        'id="modal-ohlc"',
        'id="battle-detail"',
        'id="opportunity-detail"',
        'id="score-summary"',
        'id="report"',
        'id="factors"',
        'data-opportunity-action="shadow"',
    ):
        assert marker in script

    assert "renderModalSummary(overview, klines, report" in script
    assert "this.api(`/strategy-indicators?symbol=${encoded}&tf=${timeframe}`)" in script
    assert "renderStrategyIndicators(indicatorScan)" in script
    assert "订单池深度" in script
    assert "美股映射 USDT 合约" in script
    assert "市盈率" not in script
    assert "市净率" not in script


def test_monitor_research_modal_is_responsive_and_supports_light_theme() -> None:
    stylesheet = (ROOT / "src" / "quantdesk_v2" / "static" / "monitor.css").read_text(
        encoding="utf-8"
    )

    assert ".research-modal { width: min(1420px, 98vw)" in stylesheet
    assert ".research-metrics { display: grid" in stylesheet
    assert ".research-modal .report { grid-template-columns: repeat(3" in stylesheet
    assert "@media (max-width: 620px)" in stylesheet
    assert ':host-context(html[data-theme="light"]) .research-modal' in stylesheet
