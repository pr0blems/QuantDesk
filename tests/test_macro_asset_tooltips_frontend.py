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
