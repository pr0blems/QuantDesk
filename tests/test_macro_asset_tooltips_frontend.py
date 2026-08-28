from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_macro_asset_tooltips_cover_all_proxies_and_current_direction() -> None:
    script = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(
        encoding="utf-8"
    )

    for key in ("US10Y", "US2Y", "DXY", "EQUAL_WEIGHT", "HIGH_YIELD", "OIL"):
        assert f"{key}: {{" in script
    assert 'class="macro-asset ${tone(item.change_percent)}" tabindex="0"' in script
    assert 'class="macro-asset-tooltip" role="tooltip"' in script
    assert 'aria-describedby="${this.escape(tooltipId)}"' in script
    assert 'profile[direction]' in script
    assert "历史联动总结，不代表必然因果，也不构成交易建议。" in script


def test_macro_asset_tooltip_supports_hover_focus_and_visible_overflow() -> None:
    styles = (ROOT / "src/quantdesk_v2/static/ai-monitor.css").read_text(
        encoding="utf-8"
    )

    assert ".macro-asset:hover .macro-asset-tooltip" in styles
    assert ".macro-asset:focus-visible .macro-asset-tooltip" in styles
    assert "grid-template-columns: minmax(0, 1fr)" in styles
    assert ".macro-asset-tooltip > * { grid-column: 1; }" in styles
    assert ".macro-market-footer > div:last-child { position: relative; overflow: visible; }" in styles
    assert "pointer-events: none" in styles


def test_macro_index_and_risk_cards_explain_market_impact_and_data_basis() -> None:
    script = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(
        encoding="utf-8"
    )

    for key in ("NDX", "SPX", "DJI", "RUT"):
        assert f"{key}: {{" in script
    for tooltip_id in (
        "macro-risk-impact-vix",
        "macro-risk-impact-breadth",
        "macro-risk-impact-tide",
        "macro-risk-impact-event",
    ):
        assert tooltip_id in script
    assert 'class="macro-index-card ${item.available ? "" : "unavailable"}" tabindex="0"' in script
    assert 'class="macro-asset-tooltip macro-card-tooltip" role="tooltip"' in script
    assert "数据口径" in script
    assert "点位不可直接互相比对" in script
    assert 'aria-describedby="macro-risk-impact-vix"' in script


def test_macro_index_and_risk_tooltips_support_hover_and_keyboard_focus() -> None:
    styles = (ROOT / "src/quantdesk_v2/static/ai-monitor.css").read_text(
        encoding="utf-8"
    )

    assert ".macro-index-card:hover > .macro-card-tooltip" in styles
    assert ".macro-index-card:focus-visible > .macro-card-tooltip" in styles
    assert ".macro-risk-stack > div:hover > .macro-card-tooltip" in styles
    assert ".macro-risk-stack > div:focus-visible > .macro-card-tooltip" in styles
    assert ".macro-card-tooltip { top: calc(100% + 9px)" in styles
    assert ".macro-index-card:hover, .macro-index-card:focus-visible" in styles


def test_macro_policy_labels_directional_values_as_independent_caps() -> None:
    script = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(
        encoding="utf-8"
    )

    assert "入场门槛调整" in script
    assert "多头仓位上限" in script
    assert "空头仓位上限" in script
    assert "多/空仓位上限" in script


def test_treasury_tenor_cards_explain_meaning_drivers_and_market_impact() -> None:
    script = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(
        encoding="utf-8"
    )

    for tenor in ('"2Y"', '"5Y"', '"10Y"', '"30Y"'):
        assert f"{tenor}: {{" in script
    for phrase in (
        "近端政策温度计",
        "中期周期定价",
        "全球基准折现率",
        "超长期风险定价",
        "主要驱动",
        "常见影响",
        "当前解读",
    ):
        assert phrase in script
    assert 'class="macro-yield-card ${item.available ? "" : "unavailable"}"${profile ? ` tabindex="0" aria-describedby="${this.escape(tooltipId)}"`' in script
    assert 'id="${this.escape(tooltipId)}" class="macro-asset-tooltip macro-card-tooltip" role="tooltip"' in script
    assert "不是某一只债券的实际成交收益" in script


def test_treasury_tenor_tooltips_support_hover_and_keyboard_focus() -> None:
    styles = (ROOT / "src/quantdesk_v2/static/ai-monitor.css").read_text(
        encoding="utf-8"
    )

    assert ".macro-yield-card:hover > .macro-card-tooltip" in styles
    assert ".macro-yield-card:focus-visible > .macro-card-tooltip" in styles
    assert ".macro-yield-card[tabindex]:hover" in styles
    assert ".macro-yield-card[tabindex]:focus-visible" in styles
