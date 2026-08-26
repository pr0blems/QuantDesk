from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(encoding="utf-8")
STYLES = (ROOT / "src/quantdesk_v2/static/ai-monitor.css").read_text(encoding="utf-8")


def test_opportunity_card_has_distinct_summary_details_and_actions() -> None:
    for marker in (
        'class="opportunity-identity"',
        'class="opportunity-symbol-line"',
        'class="opportunity-badges"',
        'class="opportunity-quotes"',
        'class="opportunity-top-metrics"',
        'class="opportunity-details-shell opportunity-details-primary"',
        'class="opportunity-details-shell opportunity-details-secondary"',
        'class="opportunity-details-content"',
        'class="opportunity-card-footer"',
        'class="opportunity-card-actions"',
    ):
        assert marker in SCRIPT

    assert "预测仅供研判，不构成投资建议" in SCRIPT
    assert '${positionPanel}' in SCRIPT
    assert '${signalSummaryPanel}' in SCRIPT
    assert '${conclusionControl}${detailControl}' in SCRIPT


def test_opportunity_details_toggle_updates_accessible_state() -> None:
    assert 'button.dataset.state = nextExpanded ? "expanded" : "collapsed"' in SCRIPT
    assert 'details.setAttribute("aria-hidden", String(!nextExpanded))' in SCRIPT
    assert "返回精简视图" in SCRIPT
    assert "查看完整研判" in SCRIPT
    assert 'card.querySelectorAll(".opportunity-details-shell").forEach((details) =>' in SCRIPT


def test_opportunity_card_styles_animate_and_keep_summary_visible() -> None:
    assert ".opportunity-details-shell" in STYLES
    assert "grid-template-rows: 0fr" in STYLES
    assert ".opportunity-item.is-expanded .opportunity-details-shell" in STYLES
    assert "grid-template-rows: 1fr" in STYLES
    assert ".opportunity-item.has-position:not(.is-expanded) > .opportunity-signal" in STYLES
    assert ".opportunity-item > .virtual-position" in STYLES
    assert ".opportunity-item > .opportunity-card-footer" in STYLES
    assert "@container (max-width: 680px)" in STYLES


def test_expanded_card_keeps_summary_between_primary_and_secondary_details() -> None:
    primary = SCRIPT.index('data-patch-key="details-primary"')
    signal = SCRIPT.index("${signalSummaryPanel}", primary)
    secondary = SCRIPT.index('data-patch-key="details-secondary"', signal)
    footer = SCRIPT.index('data-patch-key="card-actions"', secondary)
    assert primary < signal < secondary < footer
